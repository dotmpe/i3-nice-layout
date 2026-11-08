
def focused_window_id(tree) -> int | None:
    con = tree.find_focused()
    if not con or not getattr(con, "window", None):
        return None
    return int(con.id)


# vim:fenc=utf-8
