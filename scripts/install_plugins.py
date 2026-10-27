"""Render example identities into a separate, empty plugin installation directory."""
from pathlib import Path
import argparse,json,shutil
p=argparse.ArgumentParser();p.add_argument('--target',required=True);p.add_argument('--identities',required=True)
a=p.parse_args();source=Path(__file__).resolve().parents[1]/'plugins';target=Path(a.target).expanduser().resolve()
assert source!=target and source not in target.parents and target not in source.parents,'Use a separate installation directory outside the source tree'
identities=json.loads(Path(a.identities).read_text(encoding='utf-8'))
assert isinstance(identities,dict) and all(str(k).isdigit() and str(v).isdigit() for k,v in identities.items())
assert not any((target/d.name).exists() for d in source.iterdir() if d.is_dir()),'Target contains plugin directories; review an upgrade separately'
target.mkdir(parents=True,exist_ok=True)
for d in source.iterdir():
 if not d.is_dir():continue
 shutil.copytree(d,target/d.name)
 for f in (target/d.name).rglob('*'):
  if not f.is_file():continue
  if f.suffix and f.suffix.lower() not in ('.py','.yaml','.yml','.json','.md','.txt','.toml','.ini','.html','.css','.js','.mjs','.ts','.svg'):continue
  try:text=f.read_text(encoding='utf-8')
  except UnicodeDecodeError:continue
  rendered=text
  for old,new in identities.items():rendered=rendered.replace(str(old),str(new))
  if rendered!=text:f.write_text(rendered,encoding='utf-8')
print('Plugins copied. Configure local provider credentials and QQ permissions in AstrBot before enabling.')
