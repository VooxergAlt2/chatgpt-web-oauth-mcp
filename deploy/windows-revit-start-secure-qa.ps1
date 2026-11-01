$ErrorActionPreference='Stop'
$root=Join-Path $env:LOCALAPPDATA 'Astra-Revit-Bridge\WindowsMCP'
$startupLog=Join-Path $root 'start-secure-qa.runs.log'
function Write-StartupRecord([string]$message){
    Add-Content -LiteralPath $startupLog -Encoding UTF8 -Value ((Get-Date).ToString('o')+' '+$message)
}
trap {
    try { Write-StartupRecord ('FAILED '+$_.Exception.Message) } catch {}
    break
}
Write-StartupRecord 'BEGIN'
$secure=Join-Path $root 'secure'
$secrets=Get-Content (Join-Path $secure 'secrets.json') -Raw | ConvertFrom-Json
if($secrets.WINDOWS_MCP_AUTH_KEY.Length -lt 40 -or $secrets.WINDOWS_REVIT_GATEWAY_TOKEN.Length -lt 40){ throw 'MCP_TOKEN_VALIDATION_FAILED' }
$env:WINDOWS_MCP_AUTH_KEY=[string]$secrets.WINDOWS_MCP_AUTH_KEY
$env:WINDOWS_REVIT_GATEWAY_TOKEN=[string]$secrets.WINDOWS_REVIT_GATEWAY_TOKEN
$env:ANONYMIZED_TELEMETRY='false'
$env:WINDOWS_MCP_WATCHDOG='false'
function Get-QaVerifiedListener([int]$port,[string]$marker){
    # Get-NetTCPConnection may return a listener briefly AFTER its process exits.
    # Never mark such a stale socket as healthy and skip starting its replacement.
    for($attempt=0;$attempt -lt 8;$attempt++){
        $found=@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        if($found.Count -eq 0){return $null}
        if($found.Count -ne 1){throw ('AMBIGUOUS_QA_LISTENER_'+$port)}
        $entry=$found[0]
        if($entry.LocalAddress -ne '127.0.0.1'){throw ('NON_LOOPBACK_LISTENER_'+$port)}
        $owner=Get-CimInstance Win32_Process -Filter ("ProcessId="+$entry.OwningProcess) -ErrorAction SilentlyContinue
        if($null -eq $owner){
            Write-StartupRecord ('STALE_LISTENER_OWNER_'+$port)
            Start-Sleep -Milliseconds 350
            continue
        }
        if($owner.CommandLine -notlike ('*'+$marker+'*'))
            {throw ('REFUSE_REUSE_UNKNOWN_PROCESS_PORT_'+$port)}
        Start-Sleep -Milliseconds 350
        $confirmed=@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        if($confirmed.Count -eq 0){return $null}
        if($confirmed.Count -ne 1){throw ('AMBIGUOUS_QA_LISTENER_'+$port)}
        if($confirmed[0].LocalAddress -ne '127.0.0.1'){throw ('NON_LOOPBACK_LISTENER_'+$port)}
        if($confirmed[0].OwningProcess -ne $entry.OwningProcess){continue}
        $live=Get-CimInstance Win32_Process -Filter ("ProcessId="+$entry.OwningProcess) -ErrorAction SilentlyContinue
        if($null -ne $live -and $live.CommandLine -like ('*'+$marker+'*'))
            {return [int]$entry.OwningProcess}
        Start-Sleep -Milliseconds 350
    }
    throw ('STALE_QA_LISTENER_NOT_DRAINED_'+$port)
}
$stamp=(Get-Date).ToString('yyyyMMddHHmmss')
function Wait-QaListener([int]$port,[string]$marker,[System.Diagnostics.Process]$started){
    $deadline=(Get-Date).AddSeconds(45)
    while((Get-Date) -lt $deadline){
        if($started.HasExited){ throw ('PROCESS_EXIT_BEFORE_LISTENER_'+$port+'_EXIT_'+$started.ExitCode) }
        $owner=Get-QaVerifiedListener -port $port -marker $marker
        if($null -ne $owner){
            Write-StartupRecord ('LISTENER_READY_'+$port)
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw ('LISTENER_TIMEOUT_'+$port)
}
$py=Join-Path $root '.venv\Scripts\python.exe'
$tunnelSource=Join-Path $root 'reverse_tunnel.py'
$already=Get-CimInstance Win32_Process -Filter 'Name="python.exe"' -ErrorAction SilentlyContinue |
    Where-Object {$_.CommandLine -like '*reverse_tunnel.py*'}
if(-not $already){
    $tunnel=Start-Process -FilePath $py -ArgumentList @($tunnelSource) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root ("ssh-tunnel-"+$stamp+".stdout.log")) -RedirectStandardError (Join-Path $root ("ssh-tunnel-"+$stamp+".stderr.log"))
    Write-Output ('REVERSE_TUNNEL_LAUNCHED_PID='+$tunnel.Id)
    Write-StartupRecord ('TUNNEL_LAUNCHED_PID='+$tunnel.Id)
}else{
    Write-Output 'REVERSE_TUNNEL_ALREADY_RUNNING'
    Write-StartupRecord 'TUNNEL_REUSED'
}
$winExe=Join-Path $root '.venv-uia-e44cfb79\Scripts\windows-mcp.exe'
$winOwner=Get-QaVerifiedListener -port 8000 -marker 'windows-mcp.exe'
if($null -eq $winOwner){
    $win=Start-Process -FilePath $winExe -ArgumentList @('serve','--transport','streamable-http','--host','127.0.0.1','--port','8000','--stateless-http') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root ("windows-mcp-"+$stamp+".stdout.log")) -RedirectStandardError (Join-Path $root ("windows-mcp-"+$stamp+".stderr.log"))
    Write-Output ('WINDOWS_MCP_LAUNCH_PID='+$win.Id)
    Wait-QaListener -port 8000 -marker 'windows-mcp.exe' -started $win
}else{
    Write-Output 'WINDOWS_MCP_ALREADY_LISTENING'
    Write-StartupRecord 'LISTENER_REUSED_8000'
}
$script=Join-Path $root 'windows_revit_gateway.py'
$node=Join-Path $env:LOCALAPPDATA 'Astra-Revit-Bridge\Tools\node-v24.21.0-win-x64\node.exe'
$revit=Join-Path $env:APPDATA 'Autodesk\Revit\Addins\2023\revit_mcp_plugin\Commands\RevitMCPCommandSet\server\build\index.js'
$cert=Join-Path $secure 'gateway-cert.pem'
$key=Join-Path $secure 'gateway-key.pem'
$gatewayOwner=Get-QaVerifiedListener -port 8840 -marker 'windows_revit_gateway.py'
if($null -eq $gatewayOwner){
    $gateway=Start-Process -FilePath $py -ArgumentList @($script,'--revit-server-js',$revit,'--node-executable',$node,'--host','127.0.0.1','--port','8840','--tls-certfile',$cert,'--tls-keyfile',$key) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root ("gateway-"+$stamp+".stdout.log")) -RedirectStandardError (Join-Path $root ("gateway-"+$stamp+".stderr.log"))
    Write-Output ('GATEWAY_LAUNCH_PID='+$gateway.Id)
    Wait-QaListener -port 8840 -marker 'windows_revit_gateway.py' -started $gateway
}else{
    Write-Output 'GATEWAY_ALREADY_LISTENING'
    Write-StartupRecord 'LISTENER_REUSED_8840'
}
foreach($item in @(@{Port=8000;Marker='windows-mcp.exe'},@{Port=8840;Marker='windows_revit_gateway.py'})){
    $owner=Get-QaVerifiedListener -port $item.Port -marker $item.Marker
    if($null -eq $owner){throw ('LISTENER_MISSING_'+$item.Port)}
    Write-Output ('SECURE_LISTENER_PORT='+$item.Port+' PID='+$owner+' HOST=127.0.0.1')
}
Write-StartupRecord 'PASS'
Write-Output 'LOCAL_SECURE_MCP_START_PASS'
