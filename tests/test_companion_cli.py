"""Real installed CLI discovery, inside private fixture HOME with audit guards."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('/home/r0b0tmagic/hermes-workspace/scratch/hermes-usage-features')
EVIDENCE = Path('/home/r0b0tmagic/hermes-workspace/evidence/2026-09-12-hermes-usage-1.2.0/features')

class CompanionCli(TestCase):
    def test_discovery_enable_and_optin_false(self):
        executable=shutil.which('hermes')
        self.assertIsNotNone(executable,'Installed Hermes CLI required for this integration gate')
        # Console script interpreter, not a user-configured provider executable.
        interpreter=Path(executable).read_text().splitlines()[0].removeprefix('#!')
        with tempfile.TemporaryDirectory(prefix='cli-',dir=BASE) as tmp:
            root=Path(tmp)
            paths={'HOME':'home','HERMES_HOME':'hermes','XDG_CONFIG_HOME':'config','XDG_STATE_HOME':'state','TMPDIR':'tmp'}
            for name in paths.values(): (root/name).mkdir(mode=0o700)
            target=root/'hermes/plugins/hermes-usage-export'
            shutil.copytree(ROOT/'hermes-usage-export',target,ignore=shutil.ignore_patterns('__pycache__'))
            env={key:str(root/value) for key,value in paths.items()}
            env.update(PATH='/usr/bin:/bin',HERMES_FEATURE_FIXTURE=str(root),PYTHONDONTWRITEBYTECODE='1')
            commands=[(['plugins','enable','hermes-usage-export','--no-allow-tool-override'],0),
                      (['plugins','list','--plain','--no-bundled'],0),
                      (['usage-export','--help'],0),(['usage-export','--provider','nous'],2)]
            receipts=[]
            for args, expected in commands:
                p=subprocess.run([interpreter,'-B',str(ROOT/'tests/isolated_hermes_cli.py'),*args],
                                 env=env,cwd=root,capture_output=True,text=True,timeout=30)
                receipts.append(dict(args=args,code=p.returncode,stdout=p.stdout,stderr=p.stderr))
                self.assertEqual(p.returncode,expected,p.stderr+p.stdout)
            self.assertIn('hermes-usage-export',receipts[1]['stdout'])
            self.assertIn('--allow-network',receipts[2]['stdout'])
            self.assertIn('Refused:',receipts[3]['stdout'])
            self.assertFalse((root/'hermes/usage-export').exists())
            self.assertTrue((root/'hermes/config.yaml').exists())
            (EVIDENCE/'isolated-cli-final.json').write_text(json.dumps(receipts,indent=2))
