"""
Steam Collection Applier (v2)
==============================
Reads the reviewed recommendations file and applies approved changes
to your Steam collections JSON file.

IMPORTANT: Close Steam before running this script!

Now properly handles Steam's version counter system — each modification
gets an incrementing version number so cloud sync accepts the changes.

Usage:  python steam_applier.py
"""

import json
import os
import random
import shutil
import string
import time

# ── CONFIG ──────────────────────────────────────────────────────────────────
COLLECTIONS_JSON = r"C:\Program Files (x86)\Steam\userdata\42916257\config\cloudstorage\cloud-storage-namespace-1.json"
NAMESPACES_JSON = r"C:\Program Files (x86)\Steam\userdata\42916257\config\cloudstorage\cloud-storage-namespaces.json"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECOMMENDATIONS_FILE = os.path.join(SCRIPT_DIR, "steam_populator_recommendations.txt")

# Collections to DELETE entirely after applying individual changes
# Set to empty if you don't want to delete any
COLLECTIONS_TO_DELETE = set()
# ────────────────────────────────────────────────────────────────────────────


def load_raw_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_max_version(data):
    """Find the highest version number in the file."""
    max_ver = 0
    for entry in data:
        try:
            v = int(entry[1].get("version", "0"))
            if v > max_ver:
                max_ver = v
        except (ValueError, TypeError, IndexError):
            continue
    return max_ver


def generate_uid():
    """Generate a 12-character UID matching Steam's format."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(12))


def parse_recommendations(path):
    actions = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = [p.strip() for p in line.split("|||")]
            if len(parts) < 4:
                print(f"  [warn] Skipping malformed line {line_num}: {line[:60]}...")
                continue

            action = parts[0].upper()
            try:
                appid = int(parts[1])
            except ValueError:
                print(f"  [warn] Invalid appid on line {line_num}: {parts[1]}")
                continue

            name = parts[2]
            collection = parts[3]

            actions.append({
                "action": action,
                "appid": appid,
                "name": name,
                "collection": collection,
                "line_num": line_num,
            })

    return actions


def find_collection_entry(data, coll_name):
    """Find a collection entry by name. Returns (index, entry) or (None, None)."""
    for i, entry in enumerate(data):
        key = entry[0]
        if not key.startswith("user-collections.uc-"):
            continue
        if entry[1].get("is_deleted"):
            continue
        if "value" not in entry[1]:
            continue
        try:
            value = json.loads(entry[1]["value"])
            if value.get("name") == coll_name:
                return i, entry
        except (KeyError, json.JSONDecodeError):
            continue
    return None, None


def create_collection_entry(name, appids, version):
    """Create a new collection entry matching Steam's exact format."""
    uid = generate_uid()
    key = f"user-collections.uc-{uid}"
    value = {
        "id": f"uc-{uid}",
        "name": name,
        "added": list(appids) if appids else [],
        "removed": [],
    }
    return [key, {
        "key": key,
        "timestamp": int(time.time()),
        "value": json.dumps(value),
        "version": str(version),
    }]


def apply_changes(data, actions, start_version):
    """Apply all parsed actions to the raw JSON data."""
    stats = {"added": 0, "removed": 0, "moved": 0, "created": 0, "errors": 0}
    version = start_version

    # Group actions for efficiency
    adds = {}       # collection -> set of appids
    removes = {}    # collection -> set of appids

    for act in actions:
        action = act["action"]
        appid = act["appid"]
        coll = act["collection"]

        if action == "ADD":
            adds.setdefault(coll, set()).add(appid)
        elif action == "REMOVE":
            removes.setdefault(coll, set()).add(appid)
        elif action == "MOVE":
            if " -> " in coll:
                from_coll, to_coll = coll.split(" -> ", 1)
                removes.setdefault(from_coll.strip(), set()).add(appid)
                adds.setdefault(to_coll.strip(), set()).add(appid)
                stats["moved"] += 1
            else:
                print(f"  [warn] Invalid MOVE format: {coll}")
                stats["errors"] += 1

    # Apply REMOVES
    for coll_name, appids_to_remove in removes.items():
        idx, entry = find_collection_entry(data, coll_name)
        if idx is None:
            print(f"  [warn] Collection not found for removal: {coll_name}")
            stats["errors"] += len(appids_to_remove)
            continue

        value = json.loads(entry[1]["value"])
        original_count = len(value.get("added", []))
        value["added"] = [a for a in value.get("added", [])
                          if a not in appids_to_remove]
        removed_count = original_count - len(value["added"])

        # Bump version
        version += 1
        entry[1]["value"] = json.dumps(value)
        entry[1]["timestamp"] = int(time.time())
        entry[1]["version"] = str(version)
        stats["removed"] += removed_count

    # Apply ADDS
    for coll_name, appids_to_add in adds.items():
        if coll_name in ("NEEDS_MANUAL_REVIEW", "NEEDS_DECISION_OW"):
            continue

        idx, entry = find_collection_entry(data, coll_name)
        if idx is None:
            # Create new collection with proper version
            version += 1
            new_entry = create_collection_entry(coll_name, appids_to_add, version)
            data.append(new_entry)
            stats["created"] += 1
            stats["added"] += len(appids_to_add)
            print(f"  [new] Created collection: {coll_name} ({len(appids_to_add)} games)")
        else:
            value = json.loads(entry[1]["value"])
            existing = set(value.get("added", []))
            new_appids = appids_to_add - existing
            value["added"] = list(existing | new_appids)

            # Bump version
            version += 1
            entry[1]["value"] = json.dumps(value)
            entry[1]["timestamp"] = int(time.time())
            entry[1]["version"] = str(version)
            stats["added"] += len(new_appids)

    return stats, version


def delete_collections(data, names_to_delete, start_version):
    """Mark collections as deleted (Steam's format uses is_deleted flag)."""
    version = start_version
    deleted_count = 0

    for entry in data:
        key = entry[0]
        if not key.startswith("user-collections.uc-"):
            continue
        if entry[1].get("is_deleted"):
            continue
        if "value" not in entry[1]:
            continue
        try:
            value = json.loads(entry[1]["value"])
            if value.get("name") in names_to_delete:
                version += 1
                entry[1]["is_deleted"] = True
                entry[1]["timestamp"] = int(time.time())
                entry[1]["version"] = str(version)
                # Remove value field — deleted entries don't have it
                if "value" in entry[1]:
                    del entry[1]["value"]
                deleted_count += 1
                print(f"  [del] Deleted collection: {value['name']} "
                      f"({len(value.get('added', []))} games)")
        except (KeyError, json.JSONDecodeError):
            continue

    return deleted_count, version


def update_namespaces(namespaces_path, final_version):
    """Update the cloud-storage-namespaces.json with the new version counter."""
    if not os.path.exists(namespaces_path):
        print(f"  [warn] Namespaces file not found: {namespaces_path}")
        return

    with open(namespaces_path, "r", encoding="utf-8") as f:
        ns_data = json.load(f)

    # Format is [[1, "version"], [3, "version"]]
    for entry in ns_data:
        if entry[0] == 1:
            old_ver = entry[1]
            entry[1] = str(final_version)
            print(f"  Updated namespaces version: {old_ver} → {final_version}")
            break

    with open(namespaces_path, "w", encoding="utf-8") as f:
        json.dump(ns_data, f)


def main():
    if not os.path.exists(COLLECTIONS_JSON):
        print(f"ERROR: Collections file not found:\n  {COLLECTIONS_JSON}")
        return

    if not os.path.exists(RECOMMENDATIONS_FILE):
        print(f"ERROR: Recommendations file not found:\n  {RECOMMENDATIONS_FILE}")
        print("Run the analyzer/populator first, review the output, then run this.")
        return

    # Parse recommendations
    print("Parsing recommendations...")
    actions = parse_recommendations(RECOMMENDATIONS_FILE)
    print(f"  Found {len(actions)} approved changes.\n")

    if not actions and not COLLECTIONS_TO_DELETE:
        print("Nothing to do!")
        return

    # Backup original files
    backup_path = COLLECTIONS_JSON + ".backup"
    counter = 1
    while os.path.exists(backup_path):
        backup_path = COLLECTIONS_JSON + f".backup{counter}"
        counter += 1
    shutil.copy2(COLLECTIONS_JSON, backup_path)
    print(f"Backup saved to:\n  {backup_path}\n")

    # Load data
    data = load_raw_json(COLLECTIONS_JSON)

    # Get current max version
    max_version = get_max_version(data)
    print(f"Current max version: {max_version}")

    # Apply individual changes
    print("\nApplying changes...")
    stats, current_version = apply_changes(data, actions, max_version)
    print(f"\n  Games added to collections:      {stats['added']}")
    print(f"  Games removed from collections:  {stats['removed']}")
    print(f"  Games moved between collections: {stats['moved']}")
    print(f"  New collections created:         {stats['created']}")
    if stats["errors"]:
        print(f"  Errors/warnings:                 {stats['errors']}")

    # Delete broad collections if specified
    if COLLECTIONS_TO_DELETE:
        print(f"\nDeleting {len(COLLECTIONS_TO_DELETE)} collections...")
        deleted, current_version = delete_collections(
            data, COLLECTIONS_TO_DELETE, current_version)
        print(f"  Deleted {deleted} collections.")

    print(f"\n  Version counter: {max_version} → {current_version}")

    # Save modified data
    with open(COLLECTIONS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    # Update namespaces file with new version
    print("\nUpdating namespaces...")
    update_namespaces(NAMESPACES_JSON, current_version)

    print(f"\n{'=' * 50}")
    print(f"Done! Collections updated.")
    print(f"\nIMPORTANT NEXT STEPS:")
    print(f"  1. Open Steam")
    print(f"  2. Go to your Library")
    print(f"  3. Verify collections look correct")
    print(f"  4. If something's wrong, restore backup:")
    print(f"     Copy {os.path.basename(backup_path)}")
    print(f"     over the original file (with Steam closed)")


if __name__ == "__main__":
    main()
