"""Start-up hints: an old x86 processor gets a line about NumPy's baseline instead of a bare crash."""

from vinylcheck import preflight

OLD = "processor\t: 0\nflags\t\t: fpu vme de pse tsc msr pae mce cx8 sse sse2 ssse3 sse4a abm lm\n"
NEW = "processor\t: 0\nflags\t\t: fpu sse sse2 ssse3 sse4_1 sse4_2 popcnt avx\n"


def test_hint_only_for_old_x86_linux(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.write_text(OLD)
    new.write_text(NEW)
    h = preflight.cpu_hint(str(old), system="Linux", machine="x86_64")
    assert h and "SSE4_2" in h and 'numpy<2.4' in h
    assert preflight.cpu_hint(str(new), system="Linux", machine="x86_64") is None
    assert preflight.cpu_hint(str(old), system="Linux", machine="aarch64") is None       # not an x86 question
    assert preflight.cpu_hint(str(old), system="Darwin", machine="x86_64") is None
    assert preflight.cpu_hint(str(tmp_path / "missing"), system="Linux", machine="x86_64") is None
    preflight.run()                                                                      # this machine: no crash
