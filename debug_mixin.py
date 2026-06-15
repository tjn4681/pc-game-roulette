"""
DebugMixin for SteamRouletteAPI.

Diagnostic / debug js_api methods: dumping raw Steam Collections and
shortcut data, saving debug logs, and the native file picker.

Methods here run on the single js_api object via cooperative multiple
inheritance, so they share instance state (self.*) set up in the core
SteamRouletteAPI.__init__.
"""

import json
import os

from steam_library import parse_shortcuts_vdf


class DebugMixin:
    def debug_shortcuts(self):
        """Inspect shortcuts.vdf: does it exist, how many shortcuts, what tags?"""
        if not self._collections_path:
            return {"status": "error", "message": "No collections loaded yet."}

        config_dir = os.path.dirname(os.path.dirname(self._collections_path))
        vdf_path   = os.path.join(config_dir, "shortcuts.vdf")
        result = {
            "status":           "ok",
            "vdf_path":         vdf_path,
            "vdf_exists":       os.path.isfile(vdf_path),
            "collection_names": sorted(self._collections.keys()),
        }
        if not result["vdf_exists"]:
            return result

        try:
            with open(vdf_path, "rb") as f:
                file_size = len(f.read())
            result["vdf_size_bytes"] = file_size
        except OSError as e:
            result["read_error"] = str(e)
            return result

        shortcuts = parse_shortcuts_vdf(vdf_path)
        result["total_shortcuts"] = len(shortcuts)
        result["shortcuts"] = [
            {
                "appid": sc.get("appid"),
                "name":  (sc.get("name") or "")[:60],
                "tags":  sc.get("tags", []),
            }
            for sc in shortcuts[:30]
        ]

        all_tags = set()
        for sc in shortcuts:
            for t in sc.get("tags", []):
                all_tags.add(t)
        coll_lower = {n.lower() for n in self._collections.keys()}
        result["unique_tags"]               = sorted(all_tags)
        result["tags_matching_collections"] = sorted(t for t in all_tags if t.strip().lower() in coll_lower)
        result["tags_not_matching"]         = sorted(t for t in all_tags if t.strip().lower() not in coll_lower)
        return result

    def debug_all_keys(self):
        """Comprehensive debug: prefix breakdown + searches for where Steam
        actually stores shortcut→collection memberships."""
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(self._collections_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return {"status": "error", "message": str(e)}

        # ── Top-level prefix breakdown ─────────────────────────────────────
        by_prefix = {}
        for entry in data:
            key = entry[0]
            for sep in ("-", "."):
                if sep in key:
                    prefix = key.split(sep, 1)[0]
                    break
            else:
                prefix = key
            by_prefix.setdefault(prefix, []).append(key)

        # ── user-* sub-prefix breakdown (where the answer likely lives) ────
        user_subtypes = {}
        for entry in data:
            key = entry[0]
            if not key.startswith("user-"):
                continue
            rest = key[5:]
            sep_pos = len(rest)
            for s in (".", "-"):
                p = rest.find(s)
                if 0 < p < sep_pos:
                    sep_pos = p
            sub = "user-" + rest[:sep_pos]
            user_subtypes.setdefault(sub, []).append(key)

        # ── Any key mentioning "shortcut" or "collection" (case-insensitive) ─
        notable_keys = sorted({
            entry[0] for entry in data
            if "shortcut" in entry[0].lower() or "collection" in entry[0].lower()
        })

        # ── Search the JSON for any reference to a non-Steam shortcut appid.
        #    Wherever we find them, that's where memberships are stored.
        #    Try both unsigned and signed representations since Steam isn't
        #    consistent about it across config files. ────────────────────────
        shortcut_id_strs = set()
        for a in self._shortcuts.keys():
            shortcut_id_strs.add(str(a))                   # unsigned
            if a > 0x7FFFFFFF:
                shortcut_id_strs.add(str(a - 0x100000000)) # signed equivalent
        search_matches = []
        if shortcut_id_strs:
            for entry in data:
                key = entry[0]
                value_str = json.dumps(entry[1])
                hits = [sid for sid in shortcut_id_strs if sid in value_str]
                if hits:
                    search_matches.append({
                        "key":         key,
                        "hit_count":   len(hits),
                        "hit_samples": hits[:5],
                        "preview":     value_str[:400],
                    })

        # ── List sibling files in cloudstorage/ (other namespaces?) ────────
        folder = os.path.dirname(self._collections_path)
        folder_files = []
        small_file_contents = {}
        try:
            for fname in sorted(os.listdir(folder)):
                p = os.path.join(folder, fname)
                if os.path.isfile(p):
                    size = os.path.getsize(p)
                    folder_files.append({"name": fname, "size": size})
                    # Dump tiny files — they're probably empty markers or registries
                    if 0 < size < 5000:
                        try:
                            with open(p, "r", encoding="utf-8", errors="replace") as fp:
                                small_file_contents[fname] = fp.read()[:1500]
                        except OSError:
                            pass
        except OSError:
            pass

        # ── Probe Steam's TEXT vdf config files (where the new Collections
        #    feature likely stores shortcut memberships) ──────────────────
        parts = os.path.normpath(self._collections_path).split(os.sep)
        account_dir = None
        try:
            ud_idx = parts.index("userdata")
            account_dir = os.sep.join(parts[: ud_idx + 2])
        except (ValueError, IndexError):
            pass

        # Build a collection_id → name map so we can locate each collection's
        # membership block inside localconfig.vdf by its unique uc-XXX id.
        coll_id_to_name = {}
        for entry in data:
            key = entry[0]
            if not key.startswith("user-collections.uc-"):
                continue
            meta = entry[1]
            if meta.get("is_deleted") or "value" not in meta:
                continue
            try:
                v = json.loads(meta["value"])
                cid   = v.get("id")
                cname = (v.get("name") or "").strip()
                if cid and cname:
                    coll_id_to_name[cid] = cname
            except Exception:
                continue

        config_probe = []
        if account_dir:
            probe_paths = [
                os.path.join(account_dir, "config", "localconfig.vdf"),
                os.path.join(account_dir, "config", "sharedconfig.vdf"),
                os.path.join(account_dir, "7",      "remote", "sharedconfig.vdf"),
            ]
            collection_names = list(self._collections.keys())
            for p in probe_paths:
                info = {"path": p, "exists": os.path.isfile(p)}
                if info["exists"]:
                    info["size"] = os.path.getsize(p)
                    try:
                        with open(p, "rb") as f:
                            raw = f.read()
                        text = raw.decode("utf-8", errors="replace")
                        id_hits = [s for s in shortcut_id_strs if s in text]
                        info["shortcut_id_hits"]     = len(id_hits)
                        info["sample_id_hits"]       = id_hits[:5]
                        info["collection_name_hits"] = [n for n in collection_names if n in text]

                        # Look up each collection's uc-id in the file and dump
                        # a generous window around it so we can see the VDF
                        # structure (key path + value format).
                        coll_ctx = []
                        for cid, cname in coll_id_to_name.items():
                            idx = text.find(cid)
                            if idx < 0:
                                continue
                            start = max(0, idx - 200)
                            end   = min(len(text), idx + 3000)
                            coll_ctx.append({
                                "id":      cid,
                                "name":    cname,
                                "offset":  idx,
                                "context": text[start:end],
                            })
                            if len(coll_ctx) >= 2:  # 2 examples is plenty
                                break
                        info["collection_id_contexts"] = coll_ctx

                        # And a much larger raw window around the first
                        # shortcut id hit as a fallback if uc-id lookup misses.
                        if id_hits:
                            first = text.find(id_hits[0])
                            info["context_around_first_hit"] = text[max(0, first - 1500):first + 3500]
                    except Exception as e:
                        info["error"] = str(e)
                config_probe.append(info)

        return {
            "status":         "ok",
            "total_entries":  len(data),
            "by_prefix":      {p: len(keys) for p, keys in by_prefix.items()},
            "user_subtypes":  {p: len(keys) for p, keys in user_subtypes.items()},
            "user_examples":  {p: keys[:3]  for p, keys in user_subtypes.items()},
            "notable_keys":   notable_keys[:50],
            "shortcut_id_search": {
                "shortcut_count":      len(shortcut_id_strs),
                "matching_entries":    len(search_matches),
                "matches":             search_matches[:15],
            },
            "cloudstorage_folder":     folder,
            "cloudstorage_dir_files":  folder_files,
            "small_cloudstorage_files": small_file_contents,
            "config_probe":            config_probe,
        }

    def debug_collection(self, name):
        """Return the raw structure of one collection so we can see how Steam
        is actually storing its entries.  Used to diagnose missing shortcuts."""
        if not self._collections_path or not os.path.isfile(self._collections_path):
            return {"status": "error", "message": "No collections file loaded."}
        try:
            with open(self._collections_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            return {"status": "error", "message": str(e)}

        target = (name or "").strip().lower()
        for entry in data:
            key = entry[0]
            if not key.startswith("user-collections.uc-"):
                continue
            meta = entry[1]
            if meta.get("is_deleted") or "value" not in meta:
                continue
            try:
                value = json.loads(meta["value"])
            except (json.JSONDecodeError, KeyError):
                continue
            if value.get("name", "").strip().lower() != target:
                continue

            added = value.get("added", [])
            samples = [{"value": a, "type": type(a).__name__} for a in added[:30]]
            other_fields = {
                k: (v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} len={len(v)}>")
                for k, v in value.items() if k != "added"
            }
            return {
                "status":        "ok",
                "name":          value.get("name"),
                "keys":          list(value.keys()),
                "added_count":   len(added),
                "added_samples": samples,
                "other_fields":  other_fields,
            }

        return {"status": "notfound", "message": f"Collection {name!r} not found."}

    def save_debug_log(self, content):
        """Open a Save As dialog and write the debug log to the chosen path."""
        if self._window is None:
            return {"status": "error", "message": "Window not ready."}
        result = self._window.create_file_dialog(
            dialog_type=30,  # SAVE_DIALOG
            save_filename="pc-game-roulette-debug.txt",
            file_types=("Text files (*.txt)", "All files (*.*)"),
        )
        if not result:
            return {"status": "cancelled"}
        path = result if isinstance(result, str) else result[0]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"status": "ok", "path": path}
        except OSError as e:
            return {"status": "error", "message": str(e)}

    def browse_for_file(self):
        """Open a file dialog so the user can locate the collections file."""
        if self._window is None:
            return {"status": "error", "message": "Window not ready."}
        result = self._window.create_file_dialog(
            dialog_type=10,
            allow_multiple=False,
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        if not result:
            return {"status": "cancelled"}
        path = result[0]
        if not os.path.isfile(path):
            return {"status": "error", "message": f"File not found: {path}"}
        return self._load_from_path(path)
