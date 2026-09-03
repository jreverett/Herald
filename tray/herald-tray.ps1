# herald tray icon - shows daemon state in the Windows notification area, and
# lists the inbox so an item can be closed or deleted without a terminal.
#   green  = running (idle)   blue up-arrow = sending   purple down-arrow = receiving
#   amber breathing = an agent turn is running   red converging = a session needs you
#   grey X = daemon down / heartbeat stale
# The Inbox menu paints the items behind a red icon red, with the reason on hover.
# Reads ~/.herald/status.json and ~/.herald/activity/{send,recv} from WSL over \\wsl$.
# Run hidden:  powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File herald-tray.ps1

param(
    [string]$HeraldDir,                     # Windows path to the WSL ~/.herald dir; auto-detected if omitted
    [string]$DaemonCmd = "if systemctl --user cat herald-daemon.service >/dev/null 2>&1; then systemctl --user restart herald-daemon; else pkill -f '[h]erald.py daemon' 2>/dev/null; setsid `"`$HOME/.local/bin/herald`" daemon >/dev/null 2>&1 </dev/null & disown; fi",  # systemd if present, else the PATH wrapper - machine-agnostic
    [int]$HeartbeatTimeout = 15,         # seconds without a heartbeat => daemon considered down
    [double]$ActiveWindow = 4.0,         # seconds an arrow lingers after a send/recv event
    [int]$PulseEvery = 4,                # ticks per working-breath frame; slower than the arrows
    [string]$MenuAgent = "herald-tray",  # HERALD_AGENT for close/rm issued from this menu
    [int]$MaxInboxItems = 15             # menu entries before the list is truncated
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$iconDir = Join-Path $PSScriptRoot "icons"

function Get-BarTheme {
    # SystemUsesLightTheme = 1 => light taskbar, else dark. Default dark.
    try {
        $v = Get-ItemPropertyValue -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' `
             -Name SystemUsesLightTheme -ErrorAction Stop
        if ($v -eq 1) { return 'light' }
    } catch {}
    return 'dark'
}

# Resolve the WSL ~/.herald directory as a Windows path. At login the tray starts
# before WSL is warm, so this can return nothing; it is retried lazily in
# Poll-State rather than resolved only once - otherwise a cold boot would pin
# the icon to 'offline' forever even after WSL and the daemon come up.
function Resolve-HeraldDir {
    try {
        $prev = [Console]::OutputEncoding
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $d = (& wsl.exe -e bash -lc "wslpath -w ~/.herald" 2>$null | Select-Object -First 1)
        [Console]::OutputEncoding = $prev
        if ($d) { return $d.Trim() }
    } catch {}
    return $null
}

function Set-HeraldPaths($dir) {
    $script:heraldDir     = $dir
    $script:statusPath = if ($dir) { Join-Path $dir "status.json" } else { $null }
    $script:sendMarker = if ($dir) { Join-Path $dir "activity\send" } else { $null }
    $script:recvMarker = if ($dir) { Join-Path $dir "activity\recv" } else { $null }
}

$script:lastResolve = 0
if (-not $HeraldDir) { $HeraldDir = Resolve-HeraldDir }
Set-HeraldPaths $HeraldDir

$script:FrameCount = 8
$script:icons = @{ dark = @{}; light = @{} }
$script:frames = @{ dark = @{ send = @(); recv = @(); work = @() }; light = @{ send = @(); recv = @(); work = @() } }
foreach ($theme in 'dark', 'light') {
    foreach ($s in 'idle', 'send', 'recv', 'offline', 'work', 'blocked') {
        $script:icons[$theme][$s] = New-Object System.Drawing.Icon (Join-Path $iconDir "$theme\$s.ico")
    }
    foreach ($s in 'send', 'recv', 'work') {
        $script:frames[$theme][$s] = 0..($script:FrameCount - 1) | ForEach-Object {
            New-Object System.Drawing.Icon (Join-Path $iconDir "$theme\${s}_$_.ico")
        }
    }
}

$script:ni = New-Object System.Windows.Forms.NotifyIcon
$script:ni.Icon = $script:icons[(Get-BarTheme)]['offline']
$script:ni.Text = "herald: starting..."
$script:ni.Visible = $true

function Now-Unix { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }

function Read-Marker($path) {
    if ($path -and (Test-Path $path)) {
        try { return [double](Get-Content -Raw $path) } catch { return 0 }
    }
    return 0
}

$script:state = 'offline'

# Read status + activity markers and decide the current state + tooltip.
# Runs on a throttle (not every animation frame) to keep \\wsl$ reads light.
function Poll-State {
    $now = Now-Unix
    if (-not $script:heraldDir -and ($now - $script:lastResolve) -ge 5) {
        $script:lastResolve = $now
        Set-HeraldPaths (Resolve-HeraldDir)   # cold-boot self-heal: retry until WSL answers
    }
    $status = $null
    if ($script:statusPath -and (Test-Path $script:statusPath)) {
        try { $status = Get-Content -Raw $script:statusPath | ConvertFrom-Json } catch { $status = $null }
    }
    if (-not $status -or ($now - [double]$status.heartbeat) -gt $HeartbeatTimeout) {
        $script:state = 'offline'
        $script:ni.Text = "herald: daemon not running"
        return
    }
    $send = Read-Marker $script:sendMarker
    $recv = Read-Marker $script:recvMarker
    # Traffic is always shown: an arrow is a four-second flash over whatever the
    # resting state is, and the resting state returns as soon as it passes.
    $script:state = if ($status.blocked -gt 0) { 'blocked' }
                    elseif ($status.working -gt 0) { 'work' }
                    else { 'idle' }
    if (($now - $send) -lt $ActiveWindow -and $send -ge $recv) { $script:state = 'send' }
    elseif (($now - $recv) -lt $ActiveWindow) { $script:state = 'recv' }

    Set-Tip $status
}

# NotifyIcon.Text throws above 63 characters (verified on Windows PowerShell
# 5.1: 63 assigns, 64 throws), and the throw used to leave the tooltip frozen on
# whatever it last said. So the parts are added in priority order and the ones
# that do not fit are dropped, with the assignment guarded either way.
function Set-Tip($status) {
    $verb = @{ idle = 'running'; send = 'sending'; recv = 'receiving'
               work = 'working'; blocked = 'waiting on you' }[$script:state]
    $parts = @()
    if ($status.blocked -gt 0) { $parts += "needs you: $($status.blocked_agents -join ', ')" }
    if ($status.working -gt 0) { $parts += "working: $($status.working_agents -join ', ')" }
    if ($status.queued)        { $parts += "$($status.queued) queued" }
    $parts += "$($status.me) on $($status.listen)"
    $parts += "v$($status.version)"
    $tip = "herald: $verb"
    foreach ($p in $parts) {
        if (($tip.Length + 3 + $p.Length) -le 63) { $tip = "$tip | $p" }
    }
    try { $script:ni.Text = $tip } catch { $script:ni.Text = "herald: $verb" }
}

# Fast tick: refresh state periodically, animate send/recv, stay static otherwise.
$script:tick = 0
$script:animIdx = 0
function Update-Tray {
    if ($script:tick % 6 -eq 0) { Poll-State }
    $script:tick++
    $set = $script:icons[(Get-BarTheme)]
    if ($script:state -eq 'send' -or $script:state -eq 'recv') {
        $frames = $script:frames[(Get-BarTheme)][$script:state]
        $script:ni.Icon = $frames[$script:animIdx % $script:FrameCount]
        $script:animIdx++
    }
    elseif ($script:state -eq 'work') {
        $frames = $script:frames[(Get-BarTheme)]['work']
        $script:ni.Icon = $frames[[math]::Floor($script:animIdx / $PulseEvery) % $script:FrameCount]
        $script:animIdx++
    }
    else {
        $script:animIdx = 0
        $script:ni.Icon = $set[$script:state]
    }
}

# --- inbox menu -------------------------------------------------------------
# The listing comes from `herald inbox --json` rather than by reading
# ~/.herald/inbox over \\wsl$: whether an item is open, and which mailbox and
# agent it belongs to, are herald's rules, and a second copy of them here would
# drift and then lie during exactly the debugging this menu is for.

$script:idPattern = '^[0-9a-zA-Z._-]+$'

# WinForms menus render in the system light colours whatever the taskbar theme
# is, so one dark red serves both. Color.Empty puts a row back on the default.
$script:BlockedColour = [System.Drawing.Color]::FromArgb(176, 0, 32)
$script:NormalColour = [System.Drawing.Color]::Empty

function Invoke-Herald($heraldArgs) {
    # Returns @{ ok = bool; out = string }. Runs through a login shell so herald
    # is on PATH the same way it is for a human.
    try {
        $prev = [Console]::OutputEncoding
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        # ToString() each record rather than Out-String: herald reports failures on
        # stderr, and Out-String renders those as PowerShell NativeCommandError
        # blocks, so the balloon would show the wrapper instead of herald's message.
        $lines = & wsl.exe -e bash -lc $heraldArgs 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
        [Console]::OutputEncoding = $prev
        return @{ ok = ($code -eq 0); out = (($lines -join "`n").Trim()) }
    } catch {
        return @{ ok = $false; out = "$_" }
    }
}

function Get-Inbox {
    $r = Invoke-Herald "herald inbox --json"
    if (-not $r.ok) { return @{ ok = $false; items = @(); error = $r.out } }
    try {
        # Assign before wrapping. ConvertFrom-Json emits a JSON array as ONE object
        # down the pipeline, so @($json | ConvertFrom-Json) is a single element
        # holding the whole array - verified on Windows PowerShell 5.1. Assigning
        # first and wrapping that gives the item count.
        $parsed = $r.out | ConvertFrom-Json
        return @{ ok = $true; items = @($parsed); error = "" }
    } catch {
        return @{ ok = $false; items = @(); error = "unreadable listing: $($r.out)" }
    }
}

function Show-Balloon($title, $text) {
    $script:ni.ShowBalloonTip(4000, $title, $text, [System.Windows.Forms.ToolTipIcon]::Info)
}

function Invoke-ItemAction($item, $verb) {
    # Both close and rm need HERALD_AGENT, and close needs the item's own mailbox -
    # the default lane will not match an item that arrived on another one.
    if ($item.id -notmatch $script:idPattern -or $item.to_mailbox -notmatch $script:idPattern) {
        Show-Balloon "herald" "Refusing to act on an item with an unexpected id or mailbox."
        return
    }
    $cmd = "HERALD_AGENT='$MenuAgent' HERALD_MAILBOX='$($item.to_mailbox)' herald $verb '$($item.id)'"
    $r = Invoke-Herald $cmd
    if ($r.ok) { Show-Balloon "herald" $r.out } else { Show-Balloon "herald - failed" $r.out }
}

function Format-InboxLabel($item) {
    $who = $item.from
    if ($item.from_agent) { $who = "$($item.from) / $($item.from_agent)" }
    $lane = if ($item.to_agent) { " ->$($item.to_agent)" } else { " ->$($item.to_mailbox)" }
    $preview = $item.preview
    if ($preview.Length -gt 60) { $preview = $preview.Substring(0, 57) + "..." }
    $label = "[$($item.state)] $who$lane  $preview"
    # WinForms eats a single & as a mnemonic marker.
    return $label.Replace("&", "&&")
}

# The red icon says something is waiting on the human but not which item, and
# with fifteen rows in the menu the count alone does not answer that. herald
# decides which rows are the reason - see blocking_reason in herald.py - and
# the menu only paints what it is told, so the two can never disagree.
function Build-InboxMenu {
    $script:miInbox.DropDownItems.Clear()
    $script:miInbox.Text = "Inbox"
    $script:miInbox.ForeColor = $script:NormalColour
    $listing = Get-Inbox
    if (-not $listing.ok) {
        $miErr = $script:miInbox.DropDownItems.Add("Could not read the inbox")
        $miErr.Enabled = $false
        $miErr.ToolTipText = $listing.error
        return
    }
    if ($listing.items.Count -eq 0) {
        $script:miInbox.DropDownItems.Add("(nothing open)").Enabled = $false
        return
    }
    $blocked = @($listing.items | Where-Object { $_.blocked }).Count
    if ($blocked -gt 0) {
        $script:miInbox.Text = "Inbox ($blocked waiting on you)"
        $script:miInbox.ForeColor = $script:BlockedColour
    }
    # Blocked rows come first: the menu truncates at $MaxInboxItems, and an item
    # the parent is counting must not be one of the rows that got cut. Two Where
    # passes rather than Sort-Object, which is not stable on Windows PowerShell.
    $ordered = @($listing.items | Where-Object { $_.blocked }) +
               @($listing.items | Where-Object { -not $_.blocked })
    foreach ($item in ($ordered | Select-Object -First $MaxInboxItems)) {
        $entry = $script:miInbox.DropDownItems.Add((Format-InboxLabel $item))
        $entry.ToolTipText = "$($item.id)`nthread $($item.thread)`nreceived $($item.received)"
        if ($item.blocked) {
            $entry.ForeColor = $script:BlockedColour
            $entry.ToolTipText = "$($entry.ToolTipText)`n`nwaiting on you: $($item.blocked_reason)"
        }

        $miClose = $entry.DropDownItems.Add("Close (reversible)")
        $miClose.Tag = $item
        $miClose.add_Click({ Invoke-ItemAction $this.Tag "close" }.GetNewClosure())

        $miDelete = $entry.DropDownItems.Add("Delete permanently...")
        $miDelete.Tag = $item
        $miDelete.add_Click({
            $it = $this.Tag
            $answer = [System.Windows.Forms.MessageBox]::Show(
                "Delete $($it.id) permanently?`n`nFrom $($it.from): $($it.preview)`n`n" +
                "The record is not kept as history. It leaves herald thread, herald reply " +
                "can no longer answer it, and a delivery the sender is still retrying could " +
                "arrive again as a new item.`n`nClose instead if you only want it out of the way.",
                "herald - delete inbox item",
                [System.Windows.Forms.MessageBoxButtons]::YesNo,
                [System.Windows.Forms.MessageBoxIcon]::Warning,
                [System.Windows.Forms.MessageBoxDefaultButton]::Button2)
            if ($answer -eq [System.Windows.Forms.DialogResult]::Yes) {
                Invoke-ItemAction $it "rm"
            }
        }.GetNewClosure())
    }
    if ($listing.items.Count -gt $MaxInboxItems) {
        $more = $listing.items.Count - $MaxInboxItems
        $script:miInbox.DropDownItems.Add("... and $more more").Enabled = $false
    }
}

# Context menu: Inbox, Status balloon, Restart daemon, Exit.
$menu = New-Object System.Windows.Forms.ContextMenuStrip

# Built when the menu opens, not on the animation tick - one WSL call per
# right-click rather than eleven a second.
$script:miInbox = New-Object System.Windows.Forms.ToolStripMenuItem "Inbox"
[void]$menu.Items.Add($script:miInbox)
$menu.add_Opening({ Build-InboxMenu })
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$miStatus = $menu.Items.Add("Show status")
$miStatus.add_Click({
    $t = $script:ni.Text
    $script:ni.ShowBalloonTip(3000, "herald", $t, [System.Windows.Forms.ToolTipIcon]::Info)
})

$miRestart = $menu.Items.Add("Restart daemon")
$miRestart.add_Click({
    # Through Invoke-Herald rather than Start-Process: an -ArgumentList array is
    # joined with spaces and never quoted, so bash -lc got the single word 'if'
    # and died on a syntax error behind a hidden window. Menu items that do
    # nothing and say nothing are worse than no menu item.
    $r = Invoke-Herald $DaemonCmd
    if ($r.ok) { Show-Balloon "herald" "Daemon restarted." }
    else { Show-Balloon "herald - restart failed" $r.out }
})

$miExit = $menu.Items.Add("Exit")
$miExit.add_Click({
    $script:timer.Stop()
    $script:ni.Visible = $false
    $script:ni.Dispose()
    [System.Windows.Forms.Application]::Exit()
})

$script:ni.ContextMenuStrip = $menu
$script:ni.add_MouseDoubleClick({ $miStatus.PerformClick() })

$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = 90
$script:timer.add_Tick({ Update-Tray })
Poll-State
Update-Tray
$script:timer.Start()

[System.Windows.Forms.Application]::Run((New-Object System.Windows.Forms.ApplicationContext))
