import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("flatpakshelf", os.path.join(HERE, "..", "flatpakshelf.py"))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

INFO = """Calculator - Do math

          ID: org.gnome.Calculator
         Ref: app/org.gnome.Calculator/x86_64/stable
        Arch: x86_64
"""

LOG = """Ref: app/org.gnome.Calculator/x86_64/stable
Origin: flathub

History:
 Commit: abc123
 Subject: Update to 47.0
 Date: 2026-11-20 10:00:00 +0000

 Commit: def456
 Subject: Initial import
 Date: 2026-10-01 09:00:00 +0000
"""

KEYFILE = """[Context]
shared=network;ipc;
sockets=x11;!wayland;
filesystems=xdg-download;!host;

[Session Bus Policy]
org.foo.Bar=talk
"""


class RowTests(unittest.TestCase):
    def test_header_and_short_rows_are_dropped(self):
        out = "Name\tApplication ID\tVersion\nCalc\torg.gnome.Calculator\t47\nbroken\trow\n"
        self.assertEqual(d.parse_rows(out, 3), [["Calc", "org.gnome.Calculator", "47"]])

    def test_unused(self):
        out = "   ID   Branch\n 1.  org.freedesktop.Platform  23.08  system\n"
        self.assertEqual(d.parse_unused(out), [("org.freedesktop.Platform", "23.08")])

    def test_versions(self):
        self.assertEqual(d.parse_versions(LOG), [("abc123", "Update to 47.0", "2026-11-20 10:00:00 +0000"),
                                                 ("def456", "Initial import", "2026-10-01 09:00:00 +0000")])
        self.assertEqual(d.parse_versions("No history"), [])


class InfoTests(unittest.TestCase):
    def test_title_and_rows(self):
        title, rows = d.parse_info(INFO)
        self.assertEqual(title, "Calculator - Do math")
        self.assertEqual(rows[0], ("ID", "org.gnome.Calculator"))
        self.assertEqual(len(rows), 3)

    def test_status_text_is_not_info(self):
        self.assertIsNone(d.parse_info("Loading…"))


class PermissionTests(unittest.TestCase):
    def test_keyfile_groups(self):
        got = d.parse_keyfile(KEYFILE)
        self.assertEqual(got["Sockets"], [("X11 windowing system", "", True, ("sockets", "x11", True)),
                                          ("Wayland windowing system", "Disabled", False,
                                           ("sockets", "wayland", False))])
        self.assertEqual(got["Files"][0][:2], ("Download folder", "Read and write"))
        self.assertEqual(got["Files"][1][:2], ("All files", "Denied"))
        self.assertEqual(got["Session Bus"], [("org.foo.Bar", "Can talk to", False, None)])

    def test_override_replaces_entry(self):
        text = KEYFILE + d.OVERRIDES_MARK + "\n[Context]\nsockets=!x11;\n"
        sockets = dict(d.parse_permissions(text))["Sockets"]
        self.assertEqual(sockets[0], ("X11 windowing system", "Disabled", False, ("sockets", "x11", False), True))
        self.assertFalse(sockets[1][4])

    def test_nothing_to_show(self):
        self.assertIsNone(d.parse_permissions(""))

    def test_metadata_keeps_permission_sections(self):
        text = "[Application]\nname=org.foo.Bar\n\n[Context]\nshared=network;\n\n[Policy Foo]\nk=v\n"
        kept = d.permissions_from_metadata(text)
        self.assertIn("[Context]", kept)
        self.assertIn("[Policy Foo]", kept)
        self.assertNotIn("[Application]", kept)


class DetailTabTests(unittest.TestCase):
    def test_metadata(self):
        text = "[Application]\nname=org.foo.Bar\nruntime=org.gnome.Platform/x86_64/47\n\n[Empty]\n"
        self.assertEqual(d.parse_metadata(text), [("Application", [("name", "org.foo.Bar"),
                                                                   ("runtime", "org.gnome.Platform/x86_64/47")])])
        self.assertIsNone(d.parse_metadata("No metadata available."))

    def test_changes_from_release_notes(self):
        text = "47.0  2026-11-01\n  • Faster\n  • Fixes\n\n46.1  2026-05-02"
        self.assertEqual(d.parse_changes(text), [("47.0  2026-11-01", "• Faster\n• Fixes"),
                                                 ("46.1  2026-05-02", "")])

    def test_changes_from_commit_log(self):
        self.assertEqual(d.parse_changes(LOG)[0], ("Update to 47.0", "2026-11-20 10:00:00 +0000\nabc123"))

    def test_changes_status_text(self):
        for text in ("Loading…", "No update notes available.", ""):
            self.assertIsNone(d.parse_changes(text))


if __name__ == "__main__":
    unittest.main()
