"""Tests for xlttool/covers.py without the network: reading the serial lists and
picking the box art's name."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from xlttool import covers, games  # noqa: E402

DAT = '''clrmamepro (
\tname "Sony - PlayStation Portable"
)

game (
\tcomment "Midnight Club 3 - DUB Edition (USA)"
\trom ( serial "ULUS-10021" )
)

game (
\tname "Crash Bandicoot (Europe) (EDC)"
\tregion "Europe"
\tserial "SCES-00344"
\trom ( name "Crash Bandicoot (Europe) (EDC).bin" size 1 serial "SCES-00344" )
)
'''

NAMES = ["Midnight Club 3 - DUB Edition (USA)", "Silent Hill - Origins (Asia) (En,Fr,De,Es,It)",
         "Silent Hill - Origins (USA) (En,Fr,De,Es,It)", "Crash Bandicoot (Europe, Australia)",
         "Crash Bandicoot (USA)", "Simpsons Game, The (Europe) (En,Fr,Es,It)", "Puzzle_ Game (USA)",
         "Ratchet & Clank - Size Matters (USA) (Beta)", "Ratchet & Clank - Size Matters (USA)"]


class Covers(unittest.TestCase):
    def test_dat(self):
        d = covers.parse_dat(DAT)
        self.assertEqual(d["ULUS10021"], "Midnight Club 3 - DUB Edition (USA)")
        self.assertEqual(d["SCES00344"], "Crash Bandicoot (Europe) (EDC)")

    def test_pick(self):
        self.assertEqual(covers.pick("Midnight Club 3 - DUB Edition (USA)", NAMES), NAMES[0])
        # the game's region has no art: another region's (USA first)
        self.assertEqual(covers.pick("Silent Hill Origins (Europe) (En,Fr,De,Es,It)", NAMES), NAMES[2])
        self.assertEqual(covers.pick("Crash Bandicoot (Europe) (EDC)", NAMES), NAMES[3])
        self.assertEqual(covers.pick("The Simpsons Game", NAMES, "Europe"), NAMES[5])
        self.assertEqual(covers.pick("Puzzle: Game (USA)", NAMES), "Puzzle_ Game (USA)")
        self.assertEqual(covers.pick("Ratchet & Clank - Size Matters (USA)", NAMES), NAMES[8])
        self.assertEqual(covers.pick("Ratchet and Something Else (USA)", NAMES), "")

    def test_systems(self):
        self.assertEqual(covers.system_of(games.Game("x", "cso", "t")), "psp")
        self.assertEqual(covers.system_of(games.Game("x", "ps1", "t")), "ps1")
        self.assertEqual(covers.system_of(games.Game("x", "homebrew", "t")), "")


if __name__ == "__main__":
    unittest.main()
