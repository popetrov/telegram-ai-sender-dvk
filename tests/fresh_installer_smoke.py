"""Verify a new install with synthetic keys; no Telegram, AI or client sends."""
import argparse,os,subprocess,sys,tempfile,zipfile,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--archive',type=Path,required=True);args=parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        folder=Path(temporary)
        with zipfile.ZipFile(args.archive) as archive:archive.extractall(folder)
        code=('from pathlib import Path; import setup_wizard; '
              'setup_wizard.save(Path.cwd(),dict(number="010",TELEGRAM_API_ID="12345",'
              'TELEGRAM_API_HASH="a"*32,BOT_TOKEN="12345:"+"x"*30,OPENAI_API_KEY="test",PARSER_API_KEY="")); '
              'from dotenv import set_key; set_key(".env","TELEGRAM_API_ID","12345"); '
              'set_key(".env","TELEGRAM_API_HASH","a"*32)')
        env={**os.environ,'PYTHONIOENCODING':'utf-8'}
        subprocess.run([sys.executable,'-c',code],cwd=folder,env=env,check=True)
        subprocess.run([sys.executable,'main.py','--self-check'],cwd=folder,env=env,check=True)
        baseline=json.loads((folder/'data/installed-baseline.json').read_text('utf-8'))
        assert baseline['version.txt'].strip()==(folder/'version.txt').read_text('utf-8-sig').strip()
        print('Fresh installer: synthetic local configuration and self-check passed; no network calls.')

if __name__=='__main__':main()
