#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


BASE_DIR = Path.home() / ".chatgpt-local"
INPUT_DIR = BASE_DIR / "sessions"
OUTPUT_DIR = BASE_DIR / "archived"


def extract_user_text(input_delta):
    parts = []
    if not isinstance(input_delta, list):
        return ""

    for item in input_delta:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue

        content = item.get("content", [])
        if not isinstance(content, list):
            continue

        for c in content:
            if isinstance(c, dict) and c.get("type") == "input_text":
                text = c.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts).strip()


def remove_encrypted_content(value):
    if isinstance(value, dict):
        return {
            k: remove_encrypted_content(v)
            for k, v in value.items()
            if k != "encrypted_content"
        }
    if isinstance(value, list):
        return [remove_encrypted_content(v) for v in value]
    return value


def trim_duplicate_prefix(base_text, new_text):
    if not base_text:
        return new_text
    if not new_text:
        return ""
    if new_text.startswith(base_text):
        return new_text[len(base_text):].lstrip("\n")
    return new_text


def normalize_json_string(value):
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value
    return parsed


def normalize_delta_item(item):
    if not isinstance(item, dict):
        return item

    normalized = dict(item)
    normalized.pop("id", None)
    normalized.pop("status", None)

    if normalized.get("type") == "function_call":
        normalized["arguments"] = normalize_json_string(normalized.get("arguments"))

    return normalized


def trim_list_prefix(base_items, new_items):
    if not isinstance(new_items, list):
        return new_items
    if not isinstance(base_items, list):
        return new_items

    prefix_len = 0
    max_prefix_len = min(len(base_items), len(new_items))
    while prefix_len < max_prefix_len:
        if normalize_delta_item(base_items[prefix_len]) != normalize_delta_item(new_items[prefix_len]):
            break
        prefix_len += 1
    return new_items[prefix_len:]


def replayed_output_items(output_delta):
    if not isinstance(output_delta, list):
        return []

    items = []
    for item in output_delta:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type not in {"function_call", "function_call_output"}:
            continue

        replayed = {
            "type": item_type,
            "call_id": item.get("call_id"),
        }
        if item_type == "function_call":
            replayed["name"] = item.get("name")
            replayed["arguments"] = item.get("arguments")
        else:
            replayed["output"] = item.get("output")
        items.append(replayed)
    return items


def trim_replayed_tool_items(previous_output_delta, current_input_delta):
    if not isinstance(current_input_delta, list):
        return current_input_delta

    replayed_call_ids = {
        item.get("call_id")
        for item in replayed_output_items(previous_output_delta)
        if isinstance(item, dict) and item.get("call_id")
    }
    if not replayed_call_ids:
        return current_input_delta

    trimmed = []
    for item in current_input_delta:
        if not isinstance(item, dict):
            trimmed.append(item)
            continue
        if item.get("type") in {"function_call", "function_call_output"} and item.get("call_id") in replayed_call_ids:
            continue
        trimmed.append(item)
    return trimmed


def derive_input_delta(records, index, previous_record=None):
    current_input = records[index].get("input_delta")
    if not isinstance(current_input, list):
        return current_input

    if index == 0:
        if previous_record is None:
            return current_input
        base_input = previous_record.get("input_delta")
        base_output = previous_record.get("output_delta")
    else:
        previous_record = records[index - 1]
        base_input = previous_record.get("input_delta")
        base_output = previous_record.get("output_delta")

    current_input = trim_list_prefix(base_input, current_input)
    return trim_replayed_tool_items(base_output, current_input)


def strip_redundant_fields(record, input_delta, keep_instructions):
    cleaned = dict(record)
    if input_delta is None:
        cleaned.pop("input_delta", None)
    else:
        cleaned["input_delta"] = input_delta
    if not keep_instructions:
        cleaned.pop("instructions", None)
    cleaned.pop("_user_text", None)
    cleaned.pop("_source_file", None)
    cleaned.pop("_line_no", None)
    return cleaned


def sort_key(obj):
    return (
        obj.get("ts", ""),
        obj.get("session_id", ""),
        obj.get("_source_file", obj.get("source_file", "")),
        obj.get("_line_no", obj.get("line_no", 0)),
    )


def load_archived_groups():
    groups = []

    for path in sorted(OUTPUT_DIR.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except json.JSONDecodeError:
            continue

        merge_state = payload.get("merge_state")
        if not isinstance(merge_state, dict):
            continue

        last_record = merge_state.get("last_record")
        if not isinstance(last_record, dict):
            continue

        last_record = remove_encrypted_content(last_record)
        last_record["_source_file"] = path.name
        last_record["_line_no"] = merge_state.get("last_line_no", 0)
        last_record["_user_text"] = extract_user_text(last_record.get("input_delta"))
        groups.append({
            "group_id": payload.get("group_id", len(groups) + 1),
            "base_user_text": payload.get("base_user_text", ""),
            "latest_user_text": merge_state.get("latest_user_text", last_record.get("_user_text", "")),
            "records": [last_record],
            "archived_path": path,
            "existing_payload": payload,
            "is_archived_seed": True,
        })

    return groups


def load_records():
    records = []

    for path in sorted(INPUT_DIR.glob("*.jsonl")):
        with path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    records.append({
                        "source_file": path.name,
                        "line_no": line_no,
                        "parse_error": str(e),
                        "raw": line,
                        "_user_text": "",
                    })
                    continue

                obj = remove_encrypted_content(obj)
                obj["_source_file"] = path.name
                obj["_line_no"] = line_no
                obj["_user_text"] = extract_user_text(obj.get("input_delta"))
                records.append(obj)

    return sorted(records, key=sort_key)


def assign_groups(records, archived_groups=None):
    groups = list(archived_groups or [])

    for record in records:
        user_text = record.get("_user_text", "")
        assigned = False

        for group in groups:
            base_text = group["base_user_text"]
            if user_text and base_text and user_text.startswith(base_text):
                group["records"].append(record)
                if len(user_text) > len(base_text):
                    group["latest_user_text"] = user_text
                assigned = True
                break
            if user_text and base_text and base_text.startswith(user_text):
                group["records"].append(record)
                group["base_user_text"] = user_text
                assigned = True
                break

        if not assigned:
            groups.append({
                "group_id": len(groups) + 1,
                "base_user_text": user_text,
                "latest_user_text": user_text,
                "records": [record],
                "archived_path": None,
                "existing_payload": None,
                "is_archived_seed": False,
            })

    return groups


def build_merge_state(group):
    last_record = dict(group["records"][-1])
    last_record.pop("_user_text", None)
    last_record.pop("_source_file", None)
    last_record.pop("_line_no", None)
    return {
        "latest_user_text": group.get("latest_user_text", ""),
        "last_line_no": group["records"][-1].get("_line_no", 0),
        "last_record": last_record,
    }


def build_group_payload(group):
    existing_payload = group.get("existing_payload") or {}
    existing_steps = list(existing_payload.get("steps", []))
    records = group["records"]
    seed_count = 1 if group.get("is_archived_seed") else 0
    new_records = records[seed_count:]
    first = records[0]
    base_user_text = group.get("base_user_text", "")
    merged_steps = []
    instructions_seen = any(
        isinstance(step, dict) and "instructions" in step.get("record", {})
        for step in existing_steps
    )

    for offset, record in enumerate(new_records, len(existing_steps) + 1):
        current_user_text = record.get("_user_text", "")
        user_suffix = trim_duplicate_prefix(base_user_text, current_user_text)
        keep_instructions = not instructions_seen and "instructions" in record
        record_index = seed_count + (offset - len(existing_steps) - 1)
        previous_record = records[0] if group.get("is_archived_seed") and record_index == seed_count else None
        input_delta = derive_input_delta(records, record_index, previous_record=previous_record)
        cleaned_record = strip_redundant_fields(
            record,
            input_delta=input_delta,
            keep_instructions=keep_instructions,
        )
        if keep_instructions:
            instructions_seen = True
        merged_steps.append({
            "step": offset,
            "source_file": record.get("_source_file", record.get("source_file", "")),
            "user_text_suffix": user_suffix,
            "record": cleaned_record,
        })

    all_steps = existing_steps + merged_steps
    last_record = records[-1]
    first_session_id = existing_payload.get("first_session_id") or first.get("session_id", "")
    first_ts = existing_payload.get("first_ts") or first.get("ts", "")

    return {
        "group_id": group["group_id"],
        "base_user_text": base_user_text,
        "record_count": len(all_steps),
        "first_ts": first_ts,
        "last_ts": last_record.get("ts", existing_payload.get("last_ts", "")),
        "first_session_id": first_session_id,
        "steps": all_steps,
        "merge_state": build_merge_state(group),
    }


def write_group_files(groups):
    merged_source_files = set()

    for group in groups:
        if group.get("is_archived_seed") and len(group.get("records", [])) == 1:
            continue
        payload = build_group_payload(group)
        first_session_id = payload.get("first_session_id") or f"group_{group['group_id']:03d}"
        filename = f"{first_session_id}.json"
        path = OUTPUT_DIR / filename
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        merged_source_files.update(
            step.get("source_file")
            for step in payload.get("steps", [])
            if step.get("source_file") and step.get("source_file", "").endswith(".jsonl")
        )

    return merged_source_files


def delete_source_files(source_files):
    for source_file in sorted(source_files):
        path = INPUT_DIR / source_file
        if path.exists():
            path.unlink()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete-source-files", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archived_groups = load_archived_groups()
    records = load_records()
    groups = assign_groups(records, archived_groups=archived_groups)
    merged_source_files = write_group_files(groups)
    if args.delete_source_files:
        delete_source_files(merged_source_files)
    print(f"Wrote group files to: {OUTPUT_DIR}")
    print(f"Deleted source files: {len(merged_source_files) if args.delete_source_files else 0}")
    print(f"Total records: {len(records)}")
    print(f"Total groups: {len(groups)}")


if __name__ == "__main__":
    main()
