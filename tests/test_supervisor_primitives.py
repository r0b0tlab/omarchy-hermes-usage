import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('launch', Path(__file__).resolve().parents[1] / 'collector/launch.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Primitives(TestCase):
    def test_signal_uses_open_descriptor_and_always_closes_it(self):
        with patch.object(m.os, 'kill'), patch.object(m.os, 'pidfd_open', return_value=71) as op, \
             patch.object(m.signal, 'pidfd_send_signal', side_effect=ProcessLookupError) as sig, \
             patch.object(m.os, 'close') as close:
            m.signal_owned_child(901, 9)
            op.assert_called_once_with(901)
            sig.assert_called_once_with(71, 9)
            close.assert_called_once_with(71)
