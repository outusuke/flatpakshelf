#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Flatpakshelf: a Synaptic-style Flatpak manager (GTK4 + libadwaita).

Dependencies:
    sudo dnf install python3-gobject gtk4 libadwaita flatpak

Run:
    python3 flatpakshelf.py

Everything goes through the flatpak CLI.
"""
import gzip
import json
import operator
import os
import re
import shutil
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatchcase

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Flatpak", "1.0")
from gi.repository import Adw, Flatpak, Gio, GLib, GObject, Gtk, Pango  # noqa: E402
