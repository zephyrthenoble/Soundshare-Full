# server.ps1 - Unified SoundShare Server Manager (Backend + Frontend)
# Usage: 
#   .\server.ps1 start [backend|frontend|both] [-background]  - Start server(s) (both by default)
#   .\server.ps1 stop [backend|frontend|both]                 - Stop running server(s) (both by default)
#   .\server.ps1 restart [backend|frontend|both] [-background] - Restart server(s) (both by default)
#   .\server.ps1 status [backend|frontend|both]               - Show server status and logs (both by default)

param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action,
    
    [Parameter(Position=1)]
    [ValidateSet("backend", "frontend", "both")]
    [string]$Target = "both",
    
    [switch]$Background
)

# Server Configuration
$Servers = @{
    backend = @{
        Dir = "..\Soundshare-Backend"
        PidFile = "backend.pid"
        LogFile = "backend.log"
        Command = "uv run app.py"
        Executable = "uv"
        Ports = @(5000, 5001, 8000, 8080)
        Color = "Blue"
    }
    frontend = @{
        Dir = "..\Soundshare-Frontend"
        PidFile = "frontend.pid"
        LogFile = "frontend.log"
        Command = "npm run dev"
        Executable = "npm"
        Ports = @(3000, 3001, 4000, 8081)
        Color = "Green"
    }
}

Set-Location $PSScriptRoot

# Helper Functions
function Write-ServerMessage {
    param(
        [string]$ServerType,
        [string]$Message,
        [string]$Color = "White"
    )
    $prefix = if ($ServerType) { "[$($ServerType.ToUpper())] " } else { "" }
    Write-Host "$prefix$Message" -ForegroundColor $Color
}

function Get-ServerConfig {
    param([string]$ServerType)
    return $Servers[$ServerType]
}

function Get-ServerProcess {
    param([string]$ServerType)
    
    $config = Get-ServerConfig $ServerType
    if (Test-Path $config.PidFile) {
        $storedPid = Get-Content $config.PidFile -ErrorAction SilentlyContinue
        if ($storedPid) {
            $process = Get-Process -Id $storedPid -ErrorAction SilentlyContinue
            if ($process) {
                return $process
            } else {
                Remove-Item $config.PidFile -Force -ErrorAction SilentlyContinue
            }
        }
    }
    return $null
}

function Test-ServerRunning {
    param([string]$ServerType)
    
    # Check PID file first
    $process = Get-ServerProcess $ServerType
    if ($process) {
        return $true
    }
    
    # Check ports as backup
    $config = Get-ServerConfig $ServerType
    foreach ($port in $config.Ports) {
        try {
            if (netstat -ano | Select-String ":$port.*LISTENING") {
                return $true
            }
        } catch {}
    }
    
    return $false
}

function Stop-Server {
    param(
        [string]$ServerType,
        [bool]$Verbose = $true
    )
    
    $config = Get-ServerConfig $ServerType
    $stopped = $false
    
    if ($Verbose) {
        Write-ServerMessage $ServerType "Checking for existing processes..." "Yellow"
    }
    
    # Stop by PID file first
    $process = Get-ServerProcess $ServerType
    if ($process) {
        if ($Verbose) {
            Write-ServerMessage $ServerType "Found process (PID: $($process.Id))" "Red"
        }
        try {
            Stop-Process -Id $process.Id -Force
            if ($Verbose) {
                Write-ServerMessage $ServerType "Stopped process $($process.Id)" "Green"
            }
            Remove-Item $config.PidFile -Force -ErrorAction SilentlyContinue
            $stopped = $true
        } catch {
            if ($Verbose) {
                Write-ServerMessage $ServerType "Error stopping process: $($_.Exception.Message)" "Red"
            }
        }
    }
    
    # Stop by port scanning as fallback
    foreach ($port in $config.Ports) {
        try {
            $connections = netstat -ano | Select-String ":$port.*LISTENING"
            if ($connections) {
                $pids = $connections | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique
                foreach ($processId in $pids) {
                    if ($processId -match '^\d+$') {
                        $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
                        if ($proc) {
                            if ($Verbose) {
                                Write-ServerMessage $ServerType "Found process on port $port (PID: $processId)" "Red"
                            }
                            Stop-Process -Id $processId -Force
                            if ($Verbose) {
                                Write-ServerMessage $ServerType "Stopped process $processId" "Green"
                            }
                            $stopped = $true
                        }
                    }
                }
            }
        } catch {
            # Port check failed, continue
        }
    }
    
    if ($stopped) {
        Start-Sleep -Seconds 2
    } elseif ($Verbose) {
        Write-ServerMessage $ServerType "No existing processes found" "Green"
    }
    
    return $stopped
}

function Start-Server {
    param(
        [string]$ServerType,
        [bool]$RunInBackground
    )
    
    $config = Get-ServerConfig $ServerType
    
    # Verify directory exists
    if (-not (Test-Path $config.Dir)) {
        Write-ServerMessage $ServerType "Error: Directory not found at $($config.Dir)" "Red"
        exit 1
    }
    
    # Remove old log file
    if (Test-Path $config.LogFile) {
        Write-ServerMessage $ServerType "Removing old log file: $($config.LogFile)" "Yellow"
        try {
            Remove-Item $config.LogFile -Force -ErrorAction Stop
        } catch {
            Write-ServerMessage $ServerType "Warning: Could not remove old log file (may be in use). Will append to existing file." "Yellow"
            # If we can't remove it, we'll append to it instead by using >> in the command
        }
    }
    
    # Verify executable is available
    try {
        $version = & $config.Executable --version 2>$null
        Write-ServerMessage $ServerType "Using $($config.Executable): $version" "Cyan"
    } catch {
        Write-ServerMessage $ServerType "Error: $($config.Executable) is not available" "Red"
        exit 1
    }
    
    $mode = if ($RunInBackground) { "background" } else { "foreground" }
    Write-ServerMessage $ServerType "Starting in $mode mode..." $config.Color
    Write-ServerMessage $ServerType "Executing: $($config.Command)" "Cyan"
    Write-ServerMessage $ServerType "Output logged to: $($config.LogFile)" "Cyan"
    
    try {
        # Use >> (append) if log file couldn't be removed, otherwise use > (overwrite)
        $logRedirect = if (Test-Path $config.LogFile) { ">>" } else { ">" }
        $cmdArgs = "/c", "cd /d `"$(Resolve-Path $config.Dir)`" && $($config.Command) $logRedirect `"$(Resolve-Path '.')\$($config.LogFile)`" 2>&1"
        
        if ($RunInBackground) {
            $process = Start-Process -FilePath "cmd" -ArgumentList $cmdArgs -NoNewWindow -PassThru
            $process.Id | Out-File -FilePath $config.PidFile -Encoding ASCII
            
            Write-ServerMessage $ServerType "Started with PID: $($process.Id)" "Green"
            
            Start-Sleep -Seconds 3
            if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
                Write-ServerMessage $ServerType "Running successfully!" "Green"
            } else {
                Write-ServerMessage $ServerType "Warning: Process may have exited. Check $($config.LogFile)" "Yellow"
            }
        } else {
            Write-ServerMessage $ServerType "Press Ctrl+C to stop" "Yellow"
            Write-Host "----------------------------------------" -ForegroundColor Gray
            Start-Process -FilePath "cmd" -ArgumentList $cmdArgs -NoNewWindow -Wait
        }
    } catch {
        Write-ServerMessage $ServerType "Error starting: $($_.Exception.Message)" "Red"
        exit 1
    }
}

function Show-ServerStatus {
    param([string]$ServerType)
    
    $config = Get-ServerConfig $ServerType
    
    Write-Host "=== $($ServerType.ToUpper()) Server Status ===" -ForegroundColor $config.Color
    
    # Check process status
    $process = Get-ServerProcess $ServerType
    if ($process) {
        Write-Host "✓ Server is running (PID: $($process.Id))" -ForegroundColor Green
        Write-Host "  Process: $($process.ProcessName)" -ForegroundColor Gray
        Write-Host "  CPU Time: $($process.CPU)" -ForegroundColor Gray
        Write-Host "  Memory: $([math]::Round($process.WorkingSet64 / 1MB, 2)) MB" -ForegroundColor Gray
    } else {
        Write-Host "✗ No server process found" -ForegroundColor Yellow
    }
    
    # Check ports
    Write-Host "`nChecking ports..." -ForegroundColor Cyan
    $foundPorts = @()
    foreach ($port in $config.Ports) {
        try {
            if (netstat -ano | Select-String ":$port.*LISTENING") {
                $foundPorts += $port
                Write-Host "✓ Port $port is in use" -ForegroundColor Green
            }
        } catch {}
    }
    
    if ($foundPorts.Count -eq 0) {
        $portList = $config.Ports -join ", "
        Write-Host "✗ No ports ($portList) are listening" -ForegroundColor Yellow
    }
    
    # Show recent logs
    if (Test-Path $config.LogFile) {
        Write-Host "`nRecent log entries (last 5 lines):" -ForegroundColor Cyan
        Write-Host "----------------------------------------" -ForegroundColor Gray
        try {
            Get-Content $config.LogFile -Tail 5 | ForEach-Object {
                Write-Host $_ -ForegroundColor White
            }
        } catch {
            Write-Host "Error reading log file: $($_.Exception.Message)" -ForegroundColor Red
        }
        Write-Host "----------------------------------------" -ForegroundColor Gray
    } else {
        Write-Host "`n✗ No log file found at: $($config.LogFile)" -ForegroundColor Yellow
    }
}

function Get-TargetServers {
    param([string]$Target)
    if ($Target -eq "both") {
        return @("backend", "frontend")
    } else {
        return @($Target)
    }
}

# Main execution
Write-Host "=== SoundShare Server Manager - $($Action.ToUpper()) ($Target) ===" -ForegroundColor Magenta
Write-Host "Working directory: $(Get-Location)" -ForegroundColor Gray

$targetServers = Get-TargetServers $Target

switch ($Action.ToLower()) {
    "start" {
        $runInBackground = [bool]$Background
        if ($targetServers.Count -gt 1 -and -not $runInBackground) {
            $runInBackground = $true
            Write-Host "Multiple servers requested; launching each in background so they can start together." -ForegroundColor Cyan
            Write-Host "Use -background next time to skip this notice, or start servers individually for interactive output." -ForegroundColor Gray
            Write-Host "" 
        }

        foreach ($server in $targetServers) {
            if (Test-ServerRunning $server) {
                $process = Get-ServerProcess $server
                if ($process) {
                    Write-ServerMessage $server "Already running (PID: $($process.Id))" "Yellow"
                } else {
                    Write-ServerMessage $server "Already running (detected by port scan)" "Yellow"
                }
                Write-ServerMessage $server "Use 'restart' to restart or 'stop' to stop first" "Yellow"
            } else {
                Start-Server $server $runInBackground
            }
            if ($targetServers.Count -gt 1) { Write-Host "" }
        }
        
        if ($Target -eq "both" -and $runInBackground) {
            Write-Host "Both servers started in background!" -ForegroundColor Green
            Write-Host "Monitor: .\server.ps1 status | Stop: .\server.ps1 stop both" -ForegroundColor Yellow
        }
    }
    
    "stop" {
        $stoppedAny = $false
        foreach ($server in $targetServers) {
            $stopped = Stop-Server $server $true
            if ($stopped) {
                Write-ServerMessage $server "Stopped successfully" "Green"
                $stoppedAny = $true
            } else {
                Write-ServerMessage $server "No running server found" "Yellow"
            }
            if ($targetServers.Count -gt 1) { Write-Host "" }
        }
        
        if ($Target -eq "both" -and $stoppedAny) {
            Write-Host "All servers stopped." -ForegroundColor Green
        }
    }
    
    "restart" {
        foreach ($server in $targetServers) {
            $stopped = Stop-Server $server $true
            if ($stopped) {
                Write-ServerMessage $server "Previous server stopped" "Green"
            }
        }
        
        if ($targetServers.Count -gt 1) { Write-Host "" }
        
        $runInBackground = [bool]$Background
        if ($targetServers.Count -gt 1 -and -not $runInBackground) {
            $runInBackground = $true
            Write-Host "Multiple servers requested; launching each in background so they can start together." -ForegroundColor Cyan
            Write-Host "Use -background next time to skip this notice, or start servers individually for interactive output." -ForegroundColor Gray
            Write-Host "" 
        }

        foreach ($server in $targetServers) {
            Start-Server $server $runInBackground
            if ($targetServers.Count -gt 1) { Write-Host "" }
        }
        
        if ($Target -eq "both" -and $runInBackground) {
            Write-Host "Both servers restarted in background!" -ForegroundColor Green
        }
    }
    
    "status" {
        foreach ($server in $targetServers) {
            Show-ServerStatus $server
            if ($targetServers.Count -gt 1) { Write-Host "" }
        }
        
        Write-Host "Useful commands:" -ForegroundColor Cyan
        Write-Host "  Monitor backend: Get-Content backend.log -Wait" -ForegroundColor Gray
        Write-Host "  Monitor frontend: Get-Content frontend.log -Wait" -ForegroundColor Gray
        Write-Host "  Start both: .\server.ps1 start both [-background]" -ForegroundColor Gray
        Write-Host "  Stop both: .\server.ps1 stop both" -ForegroundColor Gray
    }
}