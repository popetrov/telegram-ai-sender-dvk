"""Manager-visible install status, no keys or diagnostic implementation prose."""
import json
from pathlib import Path

def text():
    from installation_paths import state_root,code_root
    root=state_root();version=(code_root()/'version.txt').read_text('utf-8-sig').strip()
    result='Версия программы: '+version+'\nОсновная папка: '+root.name+'\nЗапускайте программу привычным ярлыком.'
    p=root/'.dvk/legacy-inventory.json'
    if p.is_file():
        record=json.loads(p.read_text('utf-8'));rows=record.get('folders',[])
        if record.get('inspection_complete') is False:result+='\nПроверка прежних папок не завершена. Пока сохраните их.'
        safe=[Path(r['folder']).name for r in rows if r.get('safe_to_delete')]
        held=[Path(r['folder']).name for r in rows if not r.get('safe_to_delete')]
        if safe:result+='\n\nПроверены, больше не используются — можно удалить:\n'+'\n'.join(safe)
        if held:result+='\n\nПока сохраните, требуется проверка:\n'+'\n'.join(held)
    return result

def register(application,controller):
    from telegram.ext import CommandHandler
    async def show(update,context):
        if not controller(update):return
        await update.effective_message.reply_text(text())
    application.add_handler(CommandHandler('installation',show),group=-6)
