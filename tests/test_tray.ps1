# This test uses Windows Forms because the tray is a Windows-only application.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$script:checks = 0
function Assert-True($condition, $message) {
    if (-not $condition) { throw $message }
    $script:checks++
}

$trayPath = Join-Path $PSScriptRoot '..\tray\herald-tray.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $trayPath), [ref]$tokens, [ref]$parseErrors)
Assert-True ($parseErrors.Count -eq 0) 'The tray must parse without errors.'
$functions = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }

$script:commands = @()
function Invoke-Herald($command) {
    $script:commands += $command
    return $script:response
}
function Show-TextDialog($title, $text) { $script:shown = $text }
function Show-Balloon($title, $text) { $script:shown = $text }
function Now-Unix { 2000000000.0 }
$script:idPattern = '^[A-Za-z0-9_.-]+$'
$script:NormalColour = [System.Drawing.Color]::Black
$script:BlockedColour = [System.Drawing.Color]::Red
$script:miInbox = New-Object System.Windows.Forms.ToolStripMenuItem
$script:miOutgoing = New-Object System.Windows.Forms.ToolStripMenuItem
$MenuAgent = 'herald-tray'
$MaxInboxItems = 2

try {
    $script:statusPath = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString() + '.json')
    $HeartbeatTimeout = 15
    $script:ni = New-Object System.Windows.Forms.NotifyIcon
    $status = [pscustomobject]@{
        version = '0.10.1'; heartbeat = (Now-Unix); me = 'jamie'; listen = '100.89.123.31:8765'
        pid = 123; started = 'today'; queued = 2; working = 5; blocked = 1
        working_agents = @('a-very-long-agent-name', 'second', 'third', 'fourth')
        blocked_agents = @('a-very-long-blocked-agent-name')
    }
    $script:state = 'blocked'
    Set-Tip $status
    Assert-True ($script:ni.Text.Length -le 63) 'The hover tooltip must stay within its Windows limit.'
    $status | ConvertTo-Json | Set-Content $script:statusPath
    $statusClick = $ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $node.Expression.Extent.Text -eq '$miStatus' -and $node.Member.Value -eq 'add_Click'
    }, $true)[0]
    $miStatus = New-Object System.Windows.Forms.ToolStripMenuItem
    Invoke-Expression $statusClick.Extent.Text
    $miStatus.PerformClick()
    Assert-True ($script:shown -like '*0.10.1*') 'Show status must include the version even when the tooltip is full.'
    Assert-True ($script:shown.Contains('100.89.123.31:8765')) 'Show status must include the listening address.'
    Assert-True ($script:shown.Contains('Queued: 2')) 'Show status must include the queue count.'
    Assert-True ($script:shown.Contains('Working: 5')) 'Show status must include the full working count.'
    Assert-True ($script:shown.Contains('summary')) 'Capped activity lists must be labelled as summaries.'
    $status.heartbeat = (Now-Unix) - 15.5
    $status | ConvertTo-Json | Set-Content $script:statusPath
    $miStatus.PerformClick()
    Assert-True ($script:shown.Contains('stale')) 'A stale heartbeat must be explicit.'
    Assert-True ($script:shown.Contains('0.10.1')) 'A stale daemon must retain its recorded version.'
    foreach ($json in @('{}', 'null', 'broken json')) {
        Set-Content $script:statusPath $json
        $miStatus.PerformClick()
        Assert-True ($script:shown.Contains('unknown')) 'Incomplete or corrupt status must show unknown values.'
    }
    Remove-Item $script:statusPath
    $miStatus.PerformClick()
    Assert-True ($script:shown.Contains('unavailable')) 'A missing status file must be explicit.'

    $script:response = @{ ok = $true; out = '[]' }
    Build-OutgoingMenu
    Assert-True ($script:miOutgoing.DropDownItems.Count -eq 1) 'An empty outgoing list needs one row.'
    Assert-True (-not $script:miOutgoing.DropDownItems[0].Enabled) 'The empty row must be disabled.'
    Assert-True ($script:commands[-1] -eq 'herald outgoing --json') 'The outgoing menu must use CLI JSON.'

    $script:response = @{ ok = $true; out = 'broken json' }
    Build-OutgoingMenu
    Assert-True ($script:miOutgoing.DropDownItems[0].Text -eq 'Could not read outgoing items') 'Invalid JSON must show an error.'
    $script:response = @{ ok = $false; out = 'command failed' }
    Build-OutgoingMenu
    Assert-True ($script:miOutgoing.DropDownItems[0].ToolTipText -eq 'command failed') 'CLI failure must be visible.'

    $queue = [pscustomobject]@{
        id = 'queued-1'; to = 'simon'; recipient_label = 'to simon-task'; state = 'queued'
        preview = 'Check A&B'; thread = 'thread-1'; from_agent = 'jamie-task'
        age_seconds = 90; attempts = 3; last_attempt_at = 1788965000
        last_error = 'connection timed out'; retry_status = 'automatic retry pending'
    }
    $script:response = @{ ok = $true; out = (ConvertTo-Json -InputObject @($queue)) }
    Build-OutgoingMenu
    Assert-True ($script:miOutgoing.Text -eq 'Outgoing / queued (1)') 'A one-item JSON array must have count one.'
    $row = $script:miOutgoing.DropDownItems[0]
    Assert-True ($row.Text.StartsWith('simon / to simon-task [queued]')) 'Recipient and state must lead the row.'
    Assert-True ($row.Text.Contains('A&&B')) 'Ampersands must remain visible.'
    Assert-True ($row.ToolTipText.Contains('connection timed out')) 'The error must appear on hover.'
    $row.PerformClick()
    Assert-True ($script:shown.Contains('Attempts: 3')) 'Click must show the actual retry details.'
    Assert-True ($script:shown.Contains('automatic retry pending')) 'Retry status must be visible.'
    Assert-True ($script:shown.Contains('Age: 90s')) 'Queue age must be visible.'

    $waiting = $queue.PSObject.Copy()
    $waiting.state = 'awaiting_reply'
    $failed = $queue.PSObject.Copy()
    $failed.state = 'delivery_failed'
    $script:response = @{ ok = $true; out = (ConvertTo-Json -InputObject @($queue, $waiting, $failed)) }
    Build-OutgoingMenu
    Assert-True ($script:miOutgoing.DropDownItems.Count -eq 3) 'The menu must enforce the row limit.'
    Assert-True ($script:miOutgoing.DropDownItems[1].Text.Contains('[awaiting_reply]')) 'Delivered work must be distinct from queued work.'
    Assert-True ($script:miOutgoing.DropDownItems[2].Text -eq '... and 1 more') 'Truncated rows must be counted.'

    $item = [pscustomobject]@{
        id = 'inbox-1'; from = 'simon'; from_agent = 'simon-agent'; to_mailbox = 'main'
        recipient_label = 'to jamie-task'; preview = 'Task'; state = 'pending'
        blocked = $true; blocked_reason = 'recipient absent'; thread = 'thread-1'; received = 'today'
    }
    $script:response = @{ ok = $true; out = (ConvertTo-Json -InputObject @($item)) }
    Build-InboxMenu
    $row = $script:miInbox.DropDownItems[0]
    Assert-True ($row.Text.StartsWith('to jamie-task')) 'Inbox recipient must come first.'
    Assert-True ($row.ForeColor -eq $script:BlockedColour) 'A held private item must be red.'
    Assert-True ($row.DropDownItems[0].Text -eq 'View without claiming') 'Inspection must be the first action.'
    $script:response = @{ ok = $true; out = '{"text":"Full message"}' }
    $row.DropDownItems[0].PerformClick()
    Assert-True ($script:commands[-1] -eq "herald peek 'inbox-1'") 'View must call peek, not read.'
    Assert-True ($script:shown.Contains('Full message')) 'View must display the full response.'
    $script:response = @{ ok = $true; out = 'Transferred' }
    Invoke-ItemAction $item 'takeover'
    Assert-True ($script:commands[-1] -eq "HERALD_AGENT='herald-tray' HERALD_MAILBOX='main' herald takeover 'inbox-1'") 'Takeover must use the item mailbox and tray identity.'
    $item.recipient_label = 'shared mailbox main'
    Assert-True ((Format-InboxLabel $item).StartsWith('shared mailbox main')) 'Shared work must be explicit.'
    $before = $script:commands.Count
    $item.id = "bad'; echo unsafe"
    Show-InboxItem $item
    Assert-True ($script:commands.Count -eq $before) 'Malformed ids must not reach a shell.'
} finally {
    if (Test-Path $script:statusPath) { Remove-Item $script:statusPath }
    if ($miStatus) { $miStatus.Dispose() }
    if ($script:ni) { $script:ni.Dispose() }
    $script:miInbox.Dispose()
    $script:miOutgoing.Dispose()
}

if ($script:checks -lt 20) { throw 'The tray test suite did not execute its checks.' }
Write-Output "Passed $script:checks Windows tray checks."
