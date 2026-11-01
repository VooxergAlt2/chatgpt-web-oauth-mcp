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
foreach($item in @(@{Port=8000;Marker='windows-mcp.exe'},@{Port=8840;Marker='windows_revit_gateway.py'})){
    $existing=Get-NetTCPConnection -State Listen -LocalPort $item.Port -ErrorAction SilentlyContinue
    foreach($connection in @($existing)){
        $proc=Get-CimInstance Win32_Process -Filter ("ProcessId="+$connection.OwningProcess)
        if($null -eq $proc -or $proc.CommandLine -notlike ('*'+$item.Marker+'*')){throw ('REFUSE_REUSE_UNKNOWN_PROCESS_PORT_'+$item.Port)}
        if($connection.LocalAddress -ne '127.0.0.1'){throw ('NON_LOOPBACK_LISTENER_'+$item.Port)}
        Write-Output ('REUSE_QA_PORT_'+$item.Port+'_PID_'+$connection.OwningProcess)
    }
}
$stamp=(Get-Date).ToString('yyyyMMddHHmmss')
function Wait-QaListener([int]$port,[System.Diagnostics.Process]$started){
    $deadline=(Get-Date).AddSeconds(45)
    while((Get-Date) -lt $deadline){
        if($started.HasExited){ throw ('PROCESS_EXIT_BEFORE_LISTENER_'+$port+'_EXIT_'+$started.ExitCode) }
        $listeners=@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
        if($listeners.Count -gt 0){
            foreach($item in $listeners){
                if($item.LocalAddress -ne '127.0.0.1'){throw ('NON_LOOPBACK_LISTENER_'+$port)}
            }
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
if(-not (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue)){
    $win=Start-Process -FilePath $winExe -ArgumentList @('serve','--transport','streamable-http','--host','127.0.0.1','--port','8000','--stateless-http') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root ("windows-mcp-"+$stamp+".stdout.log")) -RedirectStandardError (Join-Path $root ("windows-mcp-"+$stamp+".stderr.log"))
    Write-Output ('WINDOWS_MCP_LAUNCH_PID='+$win.Id)
    Wait-QaListener -port 8000 -started $win
}else{
    Write-Output 'WINDOWS_MCP_ALREADY_LISTENING'
    Write-StartupRecord 'LISTENER_REUSED_8000'
}
$script=Join-Path $root 'windows_revit_gateway.py'
$node=Join-Path $env:LOCALAPPDATA 'Astra-Revit-Bridge\Tools\node-v24.21.0-win-x64\node.exe'
$revit=Join-Path $env:APPDATA 'Autodesk\Revit\Addins\2023\revit_mcp_plugin\Commands\RevitMCPCommandSet\server\build\index.js'
$cert=Join-Path $secure 'gateway-cert.pem'
$key=Join-Path $secure 'gateway-key.pem'
if(-not (Get-NetTCPConnection -State Listen -LocalPort 8840 -ErrorAction SilentlyContinue)){
    $gateway=Start-Process -FilePath $py -ArgumentList @($script,'--revit-server-js',$revit,'--node-executable',$node,'--host','127.0.0.1','--port','8840','--tls-certfile',$cert,'--tls-keyfile',$key) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root ("gateway-"+$stamp+".stdout.log")) -RedirectStandardError (Join-Path $root ("gateway-"+$stamp+".stderr.log"))
    Write-Output ('GATEWAY_LAUNCH_PID='+$gateway.Id)
    Wait-QaListener -port 8840 -started $gateway
}else{
    Write-Output 'GATEWAY_ALREADY_LISTENING'
    Write-StartupRecord 'LISTENER_REUSED_8840'
}
foreach($p in @(8000,8840)){
    $listeners=Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
    if(-not $listeners){throw ('LISTENER_MISSING_'+$p)}
    foreach($item in @($listeners)){
        if($item.LocalAddress -ne '127.0.0.1'){throw ('NON_LOOPBACK_LISTENER_'+$p)}
        Write-Output ('SECURE_LISTENER_PORT='+$p+' PID='+$item.OwningProcess+' HOST='+$item.LocalAddress)
    }
}
Write-StartupRecord 'PASS'
Write-Output 'LOCAL_SECURE_MCP_START_PASS'
