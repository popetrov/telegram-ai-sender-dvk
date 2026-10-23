import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for key, value in dict(TELEGRAM_API_ID="12345", TELEGRAM_API_HASH="test",
                       OPENAI_API_KEY="test", BOT_TOKEN="123:test", MANAGER_ID="test").items():
    os.environ.setdefault(key, value)

import ai
import bot
import pandas as pd
from product_catalog import ProductCatalog


class BusinessPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "prompts").mkdir()
        (self.root / "prompts/legal.md").write_text("Исследуй {{ИНН}}", encoding="utf-8")
        (self.root / "prompts/service.md").write_text("Компания {{company}}. Сайт {{website}}", encoding="utf-8")
        self.path = self.root / "products.json"
        self.products = {
            "Legal": {"prompt": "legal.md", "aliases": ["law"], "context_adapters": ["arbitration"]},
            "Service": {"prompt": "service.md", "aliases": ["services"], "version": "3",
                        "columns": {"company": "Компания", "website": "Сайт"},
                        "required_fields": ["company", "website"], "tools": [], "model": "custom-model"},
        }
        self.write()
        self.env = patch.dict(os.environ, BUSINESS_PRODUCTS_FILE=str(self.path))
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self):
        self.path.write_text(json.dumps({"schema_version": 1, "products": self.products}), encoding="utf-8")

    def catalog(self):
        return ProductCatalog(self.path, self.root / "prompts", adapters=["arbitration"])

    def test_independent_business_without_inn_through_excel_and_ai(self):
        df = pd.DataFrame([
            {"Юзернейм": "@alice", "Продукт": "services", "Компания": "Flowers", "Сайт": "https://example.com"},
            {"Юзернейм": "@bob", "Продукт": "Service", "Компания": "Studio", "Сайт": "https://example.org"},
        ])
        with patch("bot.get_existing_client_keys", return_value=(set(), set())):
            rows, duplicates = bot.classify_excel_rows(df)
        self.assertEqual(len(rows), 2)  # Empty INN must not merge unrelated leads.
        self.assertFalse(duplicates)
        for row in rows:
            self.assertIsNone(bot.validate_client_row(row))
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            output_text="ГОТОВОЕ СООБЩЕНИЕ Hello КОНЕЦ СООБЩЕНИЯ")
        with patch("ai.OpenAI", return_value=client), \
             patch("arbitration_api.find_latest_defendant_case") as api:
            result = ai.generate_company_analysis("", "", "alice", "services", fields=rows[0]["fields"])
        api.assert_not_called()
        self.assertEqual(result["product"], "Service")
        self.assertEqual(result["product_version"], "3")
        self.assertEqual(result["final_message"], "Hello")
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["tools"], [])
        self.assertEqual(kwargs["model"], "custom-model")
        self.assertEqual(kwargs["input"], "Компания Flowers. Сайт https://example.com")

    def test_non_financial_records_survive_sqlite_and_restore_without_new_ai_request(self):
        import database
        import config
        db_path = str(self.root / "local.sqlite")
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(
            output_text="ГОТОВОЕ СООБЩЕНИЕ Saved message КОНЕЦ СООБЩЕНИЯ")
        with patch.object(config, "DATABASE_PATH", db_path), \
             patch("access_runtime.require_access"), patch("ai.OpenAI", return_value=client):
            database.init_database()
            for username in ("alice", "bob"):
                row = bot.normalize_excel_row({"Юзернейм": username, "Продукт": "Service",
                                               "Компания": username, "Сайт": "https://example.com"})
                self.assertIsNone(bot.validate_client_row(row))
                status, details, item = asyncio.run(bot.prepare_one_client(row))
                self.assertEqual(status, "prepared", details)
                saved = database.get_prepared_message("", username, "Service")
                self.assertEqual(saved["username"], username)
                self.assertEqual(saved["final_message"], "Saved message")
                self.assertIn(username, saved["prompt_text"])
            self.assertEqual(client.responses.create.call_count, 2)
            status, details, item = asyncio.run(bot.prepare_one_client(row))
            self.assertIn(status, {"prepared", "restored"}, details)
            self.assertEqual(client.responses.create.call_count, 2)
            self.assertEqual(len(database.get_pending_prepared_messages()), 2)

    def test_legal_adapter_runs_only_when_configured(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output_text="ГОТОВОЕ СООБЩЕНИЕ Legal message КОНЕЦ СООБЩЕНИЯ")
        with patch("ai.OpenAI", return_value=client), \
             patch("arbitration_api.find_latest_defendant_case", return_value={"status": "NOT_FOUND", "reason": "none"}) as api:
            result = ai.generate_company_analysis("1234567890", "Name", "alice", "LAW")
        api.assert_called_once_with("1234567890")
        self.assertIn("ДАННЫЕ СУДЕБНОГО API", result["prompt_text"])
        self.assertIn("Исследуй 1234567890", result["prompt_text"])

    def test_new_product_added_without_core_changes(self):
        self.products["Another business"] = dict(self.products["Service"], aliases=["new"], version="1")
        self.write()
        self.assertEqual(ai.normalize_product("new"), "Another business")
        self.assertEqual(self.catalog().render("new", {"company": "A", "website": "B"}), "Компания A. Сайт B")

    def test_invalid_packages_fail_before_network_calls(self):
        changes = [
            {"context_adapters": ["unknown"]}, {"context_adapters": ["arbitration"]}, {"tools": ["unknown"]},
            {"prompt": "../outside.md"}, {"prompt": "/etc/passwd"},
            {"prompt": str(self.root / "prompts/service.md")}, {"prompt": "C:service.md"},
            {"aliases": ["law"]}, {"aliases": "not-a-list"},
            {"required_fields": ["missing"]}, {"columns": {"username": "Пользователь"}},
            {"model": ""}, {"version": 2}, {"typo": True},
        ]
        original = dict(self.products["Service"])
        for change in changes:
            self.products["Service"] = dict(original, **change)
            self.write()
            with self.subTest(change=change), patch("ai.OpenAI") as client:
                with self.assertRaises(ValueError):
                    ai.generate_company_analysis("", "", "alice", "Service", fields={})
                client.assert_not_called()
        self.products["Service"] = original

    def test_required_business_fields_validated_on_import(self):
        row = bot.normalize_excel_row({"Юзернейм": "alice", "Продукт": "Service", "Компания": "A"})
        self.assertIn("Сайт", bot.validate_client_row(row))

    def test_legacy_registry_and_prompt_formats(self):
        self.env.stop()
        try:
            catalog = ai.get_catalog()
            self.assertEqual(catalog.normalize("банковская гарантия"), "БГ")
            self.assertEqual(catalog.settings("Право_ИП")["context_adapters"], ["arbitration"])
            self.assertEqual(catalog.settings("Кредит")["context_adapters"], [])
            for name in catalog.products:
                text = catalog.render(name, {"inn": "1234567890", "full_name": "Name"})
                self.assertIn("1234567890", text)
        finally:
            self.env.start()

    def test_symlink_prompt_escape_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        try:
            (self.root / "prompts/escape.md").symlink_to(outside)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                self.skipTest('Windows account cannot create symlinks; exercised on Linux/macOS CI')
            raise
        self.products["Service"]["prompt"] = "escape.md"
        self.write()
        with self.assertRaises(ValueError):
            self.catalog()

    def test_schema_and_missing_template_values_rejected(self):
        with self.assertRaises(ValueError):
            self.catalog().render("Service", {"company": "A"})
        self.path.write_text(json.dumps({"schema_version": 2, "products": self.products}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.catalog()

    def test_duplicate_existing_username_is_still_filtered(self):
        df = pd.DataFrame([{"Юзернейм": "@alice", "Продукт": "Service"}])
        with patch("bot.get_existing_client_keys", return_value=(set(), {"alice"})):
            rows, duplicates = bot.classify_excel_rows(df)
        self.assertFalse(rows)
        self.assertEqual(len(duplicates), 1)


if __name__ == "__main__":
    unittest.main()
