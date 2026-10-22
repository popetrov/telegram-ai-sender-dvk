# Telegram AI Sender — macOS test setup

> This is an experimental setup guide for the `macos-support-2.0.19` branch. It is not a released or fully verified Mac installer. Keep the Windows 2.0.19 installation unchanged.

## Requirements

- macOS on Apple Silicon or Intel.
- Python 3.11 recommended.
- Internet access to install Python packages and reach Telegram, OpenAI, and GitHub.

Install Python 3.11 from the official Python downloads page:
https://www.python.org/downloads/macos/

## Prepare the project

1. Download the project ZIP from the `macos-support-2.0.19` branch in GitHub and extract it into a normal folder.
2. In Finder, duplicate `.env.example` and name the copy `.env`. If Finder hides dotfiles, use Terminal or a code editor to do this.
3. Edit `.env` with the buyer's own values. Do not reuse another manager's bot token, Telegram session, or API keys.
4. Make sure the buyer's configured product prompts are present in `prompts/`. Do not add private keys or session files to GitHub.

## Start

Open Terminal, change directory into the extracted project folder, then run:

```bash
bash start_mac.sh
```

The script creates a local virtual environment, installs dependencies, checks that `.env` exists, and starts `launcher.py`.

## Important limitations

- This branch is experimental and has not yet been tested on a physical Mac.
- First-run Telegram authorization, Excel import, message sending, scheduled sending, safe updates, and rollback must all be tested before customer use.
- Do not place `.env`, Telegram `*.session` files, or `data/` into a public ZIP or commit.
- Do not replace the existing Windows release with this branch.
