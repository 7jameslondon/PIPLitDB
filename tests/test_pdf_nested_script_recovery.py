import unittest
from scripts.extraction.record_json import _canonical_plain_for_rich


class NestedScriptRecoveryTests(unittest.TestCase):
    def test_flat_native_group_recovers_nested_scripts(self):
        self.assertEqual(_canonical_plain_for_rich('(R)H2Nγ', '(R)<sup>H<sub>2</sub>N</sup>γ'), '(R)^{H_{2}N}γ')

    def test_explicit_conflicting_script_remains_rejected(self):
        self.assertIsNone(_canonical_plain_for_rich('(R)^{H2N}γ', '(R)<sup>H<sub>2</sub>N</sup>γ'))

    def test_wrong_character_remains_rejected(self):
        self.assertIsNone(_canonical_plain_for_rich('(R)H3Nγ', '(R)<sup>H<sub>2</sub>N</sup>γ'))
