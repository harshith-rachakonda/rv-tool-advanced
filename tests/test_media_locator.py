# Unit test for pmf_media_mode._Locator (outside RV, with a stub 'rv' module).
import os, shutil, sys, tempfile, time, types

plugin_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'plugins')
rv = types.ModuleType("rv"); rvtypes = types.ModuleType("rv.rvtypes"); extra = types.ModuleType("rv.extra_commands")
class MinorMode(object):
    def init(self, *a, **k): pass
rvtypes.MinorMode = MinorMode; extra.displayFeedback = lambda *a: None
rv.rvtypes, rv.extra_commands = rvtypes, extra
sys.modules.update({"rv": rv, "rv.rvtypes": rvtypes, "rv.extra_commands": extra})
sys.path.insert(0, plugin_dir)

box = tempfile.mkdtemp(prefix="pmfmedia_")
os.environ["PMF_RV_PROFILE_DIR"] = os.path.join(box, "profile")
import pmf_media_mode as m

passed = failed = 0
def ok(what, cond, detail=""):
    global passed, failed
    if cond: passed += 1; print("PASS", what)
    else: failed += 1; print("FAIL", what, detail)

def touch(*parts):
    p = os.path.join(*parts); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").close(); return p

media = os.path.join(box, "Review Media")
m.set_media_folder(media)
ok("folder setting saved", m.media_folder() == media)
loc = m._Locator()
SERVER = r"\\10.255.255.1\Projects\FilmX"   # unreachable "file server"

real = touch(box, "local", "exists.mov")
ok("existing file left alone", loc.find(real) == real)

a = touch(media, "shot_010", "comp", "comp_v003.mov")
b = touch(media, "shot_020", "comp", "comp_v003.mov")
t0 = time.time()
r = loc.find(SERVER + r"\shot_020\comp\comp_v003.mov")
first = time.time() - t0
ok("unreachable server path -> local copy", r == b, r)
ok("same file name in two shots -> picks the matching shot", r == b and r != a)
ok("first unreachable lookup gives up quickly (%.1f s)" % first, first < 3.5)
t0 = time.time(); loc.find(SERVER + r"\shot_010\comp\comp_v003.mov"); again = time.time() - t0
ok("later lookups on that server don't wait again (%.2f s)" % again, again < 0.5)

for i in range(1001, 1011): touch(media, "shot_030", "plates", "plate.%d.exr" % i)
want = os.path.join(media, "shot_030", "plates")
for name in ("plate.1001-1010#.exr", "plate.#.exr", "plate.@@@@.exr", "plate.%04d.exr"):
    r = loc.find(SERVER + "\\shot_030\\plates\\" + name)
    ok("image sequence %s -> local folder" % name, r == os.path.join(want, name), r)

ok("file nobody has -> None", loc.find(SERVER + r"\shot_999\nothing.mov") is None)
late = touch(media, "late", "arrived_later.mov")
ok("file copied in after PMF.RV started is still found", loc.find(SERVER + r"\x\arrived_later.mov") == late)
ok("case-insensitive name match", loc.find(SERVER + r"\shot_010\COMP\COMP_V003.MOV") == a)
ok("forward-slash paths work", loc.find("//10.255.255.1/Projects/FilmX/shot_010/comp/comp_v003.mov") == a)

copies = os.path.join(media, "teams")
c1 = touch(copies, "BC CUT REF_ 04 04 2026 (1).mov")
ok("Teams copy: host '(1) (1)' finds local '(1)'", loc.find(SERVER + r"\cut\BC CUT REF_ 04 04 2026 (1) (1).mov") == c1)
ok("Teams copy: host '(2)' finds local '(1)'", loc.find(SERVER + r"\cut\BC CUT REF_ 04 04 2026 (2).mov") == c1)
ok("Teams copy: host plain name finds local '(1)'", loc.find(SERVER + r"\cut\BC CUT REF_ 04 04 2026.mov") == c1)
ok("Teams copy: host '(1) (2)' finds local '(1)'", loc.find(SERVER + r"\cut\BC CUT REF_ 04 04 2026 (1) (2).mov") == c1)
exact = touch(copies, "BC CUT REF_ 04 04 2026 (1) (1).mov")
ok("exact name still wins over a numbered copy", loc.find(SERVER + r"\cut\BC CUT REF_ 04 04 2026 (1) (1).mov") == exact)
ok("different file is not matched", loc.find(SERVER + r"\cut\OTHER CUT (1).mov") is None)
ok("numbered name with different extension is not matched", loc.find(SERVER + r"\cut\BC CUT REF_ 04 04 2026 (1).mp4") is None)
ok("name that is only '(1).mov' doesn't match everything", loc.find(SERVER + r"\cut\(3).mov") is None)
shutil.rmtree(box, ignore_errors=True)
print("RESULT: %d passed, %d failed" % (passed, failed))
