"""Run behavioral tests with temporary data and no real QQ transport."""
from pathlib import Path
import os,subprocess,sys,tempfile
r=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as tmp:
 work=Path(tmp);(work/'data/plugins').mkdir(parents=True)
 env=dict(os.environ,ASTRBOT_ROOT=str(work),REFACTOR_STAGE=str(r/'plugins'),PYTHONPATH=os.pathsep.join([str(work),str(r/'framework'),str(r/'plugins'),os.environ.get('PYTHONPATH','')]))
 for name in (sys.argv[1:] or ('test_persona_tuning.py','test_evening_features.py','test_persona_native.py','test_evening_native.py')):
  subprocess.run([sys.executable,str(r/'tests/run_isolated.py'),str(r/'tests'/name),str(r/'plugins')],cwd=work,env=env,check=True)
