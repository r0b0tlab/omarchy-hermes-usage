"""Bounded standalone Quickshell fixture on the real compositor, never reloads shell.

All mutable fixture state is under scratch. A uniquely owned /tmp directory
only shortens Quickshell's Unix socket path; it is removed in finally.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('/home/r0b0tmagic/hermes-workspace/scratch/hermes-usage-features')
EVIDENCE = Path('/home/r0b0tmagic/hermes-workspace/evidence/2026-09-12-hermes-usage-1.2.0/features')

def run(command, env, timeout=5):
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)

def until(fn, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value: return value
        time.sleep(.05)
    raise AssertionError('fixture deadline exceeded')

def main():
    root = Path(tempfile.mkdtemp(prefix='ui-', dir=BASE))
    (root/'runtime').mkdir(mode=0o700)
    alias = Path(tempfile.mkdtemp(prefix='hu-ui-', dir='/tmp'))
    (alias/'r').symlink_to(root/'runtime', target_is_directory=True)
    proc = None; receipts = []; original = {}
    hostenv = {k: os.environ[k] for k in ('HYPRLAND_INSTANCE_SIGNATURE', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR') if k in os.environ}
    hostenv.update(PATH='/usr/bin', HOME=str(root/'home'), XDG_CONFIG_HOME=str(root/'config'),
                   XDG_STATE_HOME=str(root/'state'), HERMES_HOME=str(root/'hermes'), TMPDIR=str(root/'tmp'))
    env = dict(hostenv, XDG_RUNTIME_DIR=str(alias/'r'), WAYLAND_DISPLAY='/run/user/1000/wayland-1',
               QT_QPA_PLATFORM='wayland', QS_NO_RELOAD_POPUP='1')
    for name in ('home','config','state','hermes','tmp'): (root/name).mkdir()
    state = root/'state/omarchy/agents/usage/hermes.json'; state.parent.mkdir(parents=True)
    shutil.copyfile(ROOT/'Details.qml', root/'Details.qml')
    assert (ROOT/'Details.qml').read_bytes() == (root/'Details.qml').read_bytes()
    code_hash = hashlib.sha256((root/'Details.qml').read_bytes()).hexdigest()
    shutil.copyfile(ROOT/'tests/details-shell.qml', root/'shell.qml')
    shutil.copytree('/usr/share/omarchy/shell/Commons', root/'Commons')
    (root/'imports').mkdir(); (root/'imports/qs').symlink_to(root, target_is_directory=True)
    env['QML_IMPORT_PATH'] = str(root/'imports')
    log = (root/'qs.log').open('wb')
    def ipc(name, check=True):
        r = run(['/usr/bin/qs','ipc','--pid',str(proc.pid),'call','usage-fixture',name],env)
        if check: assert r.returncode == 0, r.stderr
        return r.stdout.strip()
    def clients():
        r = run(['hyprctl','clients','-j'],hostenv)
        return [c for c in json.loads(r.stdout) if c['pid'] == proc.pid]
    def dispatch(action, arg):
        if action == 'setfloating': expr = 'hl.dsp.window.float({action="set",window=' + json.dumps(arg) + '})'
        elif action == 'focuswindow': expr = 'hl.dsp.focus({window=' + json.dumps(arg) + '})'
        elif action == 'closewindow': expr = 'hl.dsp.window.close({window=' + json.dumps(arg) + '})'
        elif action == 'resizewindowpixel':
            dims, address = arg.split(','); _, x, y = dims.split()
            expr = 'hl.dsp.window.resize({x='+str(int(x))+',y='+str(int(y))+',relative=false,window='+json.dumps(address)+'})'
        elif action == 'sendshortcut':
            _, key, address = arg.split(',')
            expr = 'hl.dsp.send_shortcut({mods="",key='+json.dumps(key)+',window='+json.dumps(address)+'})'
        else: raise ValueError(action)
        r = run(['hyprctl','eval','hl.dispatch('+expr+')'],hostenv)
        assert r.returncode == 0, (action, arg, r.stdout, r.stderr)
    def status(): return json.loads(ipc('status'))
    try:
        original = json.loads(run(['hyprctl','activewindow','-j'],hostenv).stdout)
        proc = subprocess.Popen(['/usr/bin/qs','-p',str(root),'--no-color'],env=env,stdout=log,stderr=subprocess.STDOUT)
        until(lambda: ipc('ping',False) == 'ready')
        b = dict(rows=2,calls=3,unknownCallRows=0,tokens=450,reasoning=80,cacheRead=0,
                 estimatedUsd=.30,actualUsd=.10,latestStatusRows=dict(estimated=0,actual=1,included=1,unknown=0))
        base = dict(id='hermes',hasLocalStats=True,todayTotalTokens=450,todayPrompts=2,todaySessions=1,
                    details=dict(truncated=False,totals=b,providers={'nous':b},tasks={'ordinary':b}),accounts=[])
        from test_quota import load
        q = load('quota_io')
        now = float(int(time.time()))
        a = q.unavailable('openai-codex',now)
        a.update(available=True,status='observed',plan='pro',windows=[dict(label='Session',usedPercent=80,remainingPercent=20,resetAt=now+300)])
        cases = {'empty':dict(id='hermes',hasLocalStats=False,details={},accounts=[]), 'stats':copy.deepcopy(base)}
        unknown = copy.deepcopy(base)
        for group in [unknown['details']['totals'],unknown['details']['providers']['nous'],unknown['details']['tasks']['ordinary']]:
            group.update(estimatedUsd=None,actualUsd=None,calls=None,unknownCallRows=2,
                         latestStatusRows=dict(estimated=0,actual=0,included=0,unknown=2))
        cases['unknowncost'] = unknown
        partial=copy.deepcopy(base); partial['details']['truncated']=True; cases['partial']=partial
        multi=copy.deepcopy(base); multi['accounts']=[a,dict(q.unavailable('nous',now),remainingUsd=12.50,currency='USD',available=True,status='observed',accessStatus='member-cap-exceeded')]; cases['multiplequota']=multi
        expired=copy.deepcopy(base); expired['accounts']=[dict(a,windows=[dict(a['windows'][0],resetAt=now-1)])]; cases['expired']=expired
        longlabels=copy.deepcopy(base)
        longlabels['details']['tasks']={'very_long_task_name_'*4:copy.deepcopy(b)}
        cases['longlabels']=longlabels
        for name, record in cases.items():
            record['updatedAt']='Fixture: '+name
            state.write_text(json.dumps(record))
            ipc('open'); until(lambda: status().get('updatedAt') == record['updatedAt'])
            client=until(clients)[0]; address='address:'+client['address']
            dispatch('setfloating',address); dispatch('resizewindowpixel','exact 960 980,'+address)
            dispatch('focuswindow',address)
            time.sleep(.35)
            shot=EVIDENCE/(name+'-full.png')
            r=run(['grim',str(shot)],hostenv); assert r.returncode == 0,r.stderr
            client=clients()[0]
            x,y=client['at']; w,h=client['size']
            r=run(['magick',str(shot),'-crop',f'{w}x{h}+{x}+{y}','+repage',str(EVIDENCE/(name+'.png'))],hostenv)
            assert r.returncode == 0,r.stderr
            receipts.append(dict(case=name,status=status(),geometry=dict(at=client['at'],size=client['size'])))
            if name == 'multiplequota':
                assert status()['windows'] == [1,0]
                assert ipc('expiry') == '0'
            if name == 'expired': assert status()['windows'] == [0]
            ipc('hide'); assert not status()['opened']
        # Behavioral lifecycle and injected-service checks, not shell subprocesses.
        ipc('open'); until(clients)
        assert ipc('refresh') == '1'; assert ipc('busy') == '1'
        client=clients()[0]; address='address:'+client['address']
        dispatch('closewindow',address)
        until(lambda: not status()['opened'])
        assert status()['hides'] >= 1
        ipc('open'); until(clients)
        address='address:'+clients()[0]['address']
        dispatch('sendshortcut',',Escape,'+address)
        until(lambda: not status()['opened'])
        ipc('open'); until(clients)
        dispatch('resizewindowpixel','exact 460 600,address:'+clients()[0]['address'])
        time.sleep(.2)
        run(['grim',str(EVIDENCE/'resized-full.png')],hostenv)
        receipts.append(dict(lifecycle='native-close escape reopen resize refresh busy',status=status()))
        ipc('hide')
    finally:
        if proc is not None:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
        log.close()
        shutil.copyfile(root/'qs.log',EVIDENCE/'ui-runtime.log')
        shutil.rmtree(alias)
        if original.get('address'): dispatch('focuswindow','address:'+original['address'])
        (EVIDENCE/'ui-receipt.json').write_text(json.dumps(dict(root=str(root),detailsSha256=code_hash,receipts=receipts),indent=2))
    text=(root/'qs.log').read_text()
    assert not any(s in text for s in ('ReferenceError','TypeError','Failed to load configuration')),text
    print(json.dumps(dict(root=str(root),cases=len(cases),lifecycle='passed')))

if __name__ == '__main__': main()
