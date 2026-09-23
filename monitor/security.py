def mask_endpoint(value):
    """Mask infrastructure details for non-administrator presentation."""
    value = str(value or "").strip()
    if not value:
        return "—"
    parts = value.split(".")
    if len(parts) >= 4 and all(part.isdigit() for part in parts):
        return ".".join(parts[:2] + ["•••", "•••"])
    if len(parts) > 1:
        first = parts[0]
        visible = first[:min(5, max(2, len(first) // 2))]
        return visible + "••••" + "." + ".".join(parts[1:])
    if len(value) <= 4:
        return "••••"
    return value[:2] + "••••" + value[-2:]

