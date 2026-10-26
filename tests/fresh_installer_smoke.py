"""Verify a new install before API provisioning; no Telegram or client sends."""
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
              'TELEGRAM_API_HASH="a"*32,BOT_TOKEN="12345:"+"x"*30,OPENAI_API_KEY="test",PARSER_API_KEY=""))')
        env={**os.environ,'PYTHONIOENCODING':'utf-8'}
        env.pop('TELEGRAM_API_ID',None)
        env.pop('TELEGRAM_API_HASH',None)
        subprocess.run([sys.executable,'-c',code],cwd=folder,env=env,check=True)
        settings=(folder/'.env').read_bytes()
        assert b'TELEGRAM_API_ID=' not in settings
        assert b'TELEGRAM_API_HASH=' not in settings
        subprocess.run([sys.executable,'main.py','--self-check'],cwd=folder,env=env,check=True)
        assert (folder/'.env').read_bytes()==settings
        baseline=json.loads((folder/'data/installed-baseline.json').read_text('utf-8'))
        assert baseline['version.txt'].strip()==(folder/'version.txt').read_text('utf-8-sig').strip()
        print('Fresh installer: synthetic local configuration and self-check passed; no network calls.')

if __name__=='__main__':main()
