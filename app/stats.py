from app.models import Order, ReworkRecord


def parse_int_safe(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def summarize_rework_by_blame(records: list[ReworkRecord]) -> list[dict]:
    groups: dict[str, dict] = {}
    for record in records:
        blame = record.blame or "не вказано"
        group = groups.setdefault(blame, {"blame": blame, "count": 0, "redo_quantity_sum": 0})
        group["count"] += 1
        qty = parse_int_safe(record.redo_quantity)
        if qty is not None:
            group["redo_quantity_sum"] += qty
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)


def summarize_by_material(orders: list[Order]) -> list[dict]:
    """Orders and unit totals per production material for the period. Skips the
    non-production "Не матеріал" bucket; folds orders whose material is still
    unresolved into a trailing "Не визначено" group so the operator can see how
    much colour text isn't classified yet."""
    groups: dict[object, dict] = {}
    for order in orders:
        material = order.material
        if material is None:
            key, name, sort_order = ("__unresolved__", "Не визначено", 1000)
        elif not material.is_production:
            continue
        else:
            key, name, sort_order = (material.id, material.name, material.sort_order)
        group = groups.setdefault(
            key, {"material": name, "sort_order": sort_order, "order_count": 0, "quantity_sum": 0}
        )
        group["order_count"] += 1
        qty = parse_int_safe(order.quantity)
        if qty is not None:
            group["quantity_sum"] += qty
    return sorted(groups.values(), key=lambda g: (g["sort_order"], -g["quantity_sum"]))


def average_new_to_milled_hours(orders: list[Order]) -> float | None:
    durations = []
    for order in orders:
        new_at_times = [e.occurred_at for e in order.status_events if e.status == "нове"]
        milled_at_times = [e.occurred_at for e in order.status_events if e.status == "відфрезеровано"]
        if not new_at_times or not milled_at_times:
            continue
        new_at = min(new_at_times)
        milled_at = min(milled_at_times)
        durations.append((milled_at - new_at).total_seconds() / 3600)

    if not durations:
        return None
    return sum(durations) / len(durations)
