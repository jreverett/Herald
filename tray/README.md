# herald tray icon (Windows)

A notification-area icon that shows daemon state at a glance, reading the
heartbeat and activity markers the daemon writes to `~/.herald`.

| Icon | State |
|------|-------|
| up-chevrons `⌃⌃` (foreground colour) | running, idle |
| right-chevrons `››` (blue) | sending to a peer |
| left-chevrons `‹‹` (green) | receiving from a peer |
| up-chevrons breathing (amber) | an agent is working on herald work |
| converging chevrons `› ‹` (red) | herald is waiting on you |
| down-chevrons (grey) | daemon down / heartbeat stale |

The breath keeps idle's shape because a running turn is a state, not a
direction, and traffic takes precedence over both resting states - an arrow is a
four-second flash over whatever was showing. Red converges the chevron pair, a
direction neither send nor receive uses, so it stays distinguishable in
greyscale; the tooltip names what is waiting even while an arrow covers the
icon. Red is read from the inbox - an `accepted` task, or an item nothing is
listening for - not from the harness, so an unrelated permission prompt in a
session that also does herald work does not raise it.

The tooltip must stay within 63 characters: `NotifyIcon.Text` throws above that,
and the throw would leave the tooltip frozen on whatever it last said. It is
built in priority order and the parts that do not fit are dropped. It reflects an agent actually
spending tokens, not a claimed inbox item - a session waiting on a human answer
reads as idle. See the `activity` section of the main README for the hooks that
drive it; without them the icon simply never breathes.

Direction is the primary signal, colour secondary, so the states stay
distinguishable in greyscale (colour-blind safe). Design: `icons/src/DESIGN.md`
("Two Roofs"). Regenerate the `.ico` sets with `python3 gen_icons.py` (needs
Pillow); it emits a `dark/` and a `light/` set and the tray picks the one that
matches the current taskbar theme.

## Run it

```powershell
powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File herald-tray.ps1
```

The script auto-detects the WSL `~/.herald` path via `wslpath`. Right-click the
icon for **Inbox / Show status / Restart daemon / Exit**; double-click shows a
status balloon.

## The Inbox menu

The **Inbox** submenu lists open items and offers two actions on each, which is
usually quicker than a terminal when you are debugging routing.

When the icon is red, the items that are the reason are red too, the parent
entry reads **Inbox (N waiting on you)**, and hovering a red row gives the
reason - an `accepted` task that owes an answer, or an item on a mailbox nothing
is listening to. They are listed first, so the 15-item cap cannot hide one that
the count includes. Which rows those are is decided by herald itself
(`blocking_reason`, returned as `blocked` and `blocked_reason` by
`herald inbox --json`); the menu only paints what it is told, so the tooltip
count and the marked rows cannot disagree.

- **Close (reversible)** runs `herald close`. The item leaves the list, the
  record is kept as history, and `herald reopen` puts it back.
- **Delete permanently...** runs `herald rm` behind a confirmation box. The
  record is not kept: the item leaves `herald thread`, `herald reply` can no
  longer answer it, and a delivery the sender is still retrying could arrive
  again as a new item.

The list is built when the menu opens, not on the animation tick, so it costs one
`herald inbox --json` call per right-click. Actions run as `HERALD_AGENT`
`herald-tray` and pass each item's own mailbox, because the default lane does not
match an item that arrived on another one. Results and failures appear as a
balloon. Pass `-MenuAgent` or `-MaxInboxItems` to change the agent name or the
15-item cap.

## Auto-start on login

The main `install.sh` does this automatically on WSL-with-Windows, so a normal
install already gives you the icon at every login. To manage it by hand:

```powershell
powershell -ExecutionPolicy Bypass -File setup-tray.ps1 enable    # start at login + now
powershell -ExecutionPolicy Bypass -File setup-tray.ps1 disable   # remove it
powershell -ExecutionPolicy Bypass -File setup-tray.ps1 status
```

`enable` installs a hidden VBScript launcher (`herald-tray.vbs`) in the Startup
folder and starts the tray immediately. The `.vbs`/`wscript` indirection is
deliberate: a detached `Start-Process`/shortcut to `powershell.exe` launched from
WSL or a script does not attach to the interactive desktop, so its icon paints to
an invisible window station. wscript spawns the tray as a child that inherits the
visible desktop, so the icon actually appears - both at login and right away.
