# Latex-Copilot installer for Windows 10 (1809 or later) and Windows 11.
#
#   irm https://raw.githubusercontent.com/Vitex-Lucens/latex_AI_copilot/main/install.ps1 | iex
#   double-click install.cmd    in a copy of the repository (git clone or zip)
#   install.cmd -Uninstall      remove the app (Python, TeX, Ghostscript and Claude Code stay installed)
#
# It installs whatever is missing, asking before each step: Python, MiKTeX (TeX) and Strawberry Perl (which
# latexmk needs) with winget, Ghostscript with its official installer, and Claude Code with Anthropic's.
# Then it installs the app into %LOCALAPPDATA%\latex-copilot, with a `latex-copilot` command.
# Run it again at any time to update. The app itself needs no admin rights.
#
# Settings (environment variables; -Yes, -SkipPrereqs and -Uninstall also work after install.cmd):
#   LATEX_COPILOT_YES=1            answer yes to every question
#   LATEX_COPILOT_SKIP_PREREQS=1   install only the app
#   LATEX_COPILOT_NO_CLAUDE=1      don't offer to install Claude Code
#   LATEX_COPILOT_REF=<branch|tag> the version to download (default: main)
#   LATEX_COPILOT_SOURCE=<folder or .zip URL>   install from there instead of GitHub
#
# Written for Windows PowerShell 5.1 (built into Windows), ASCII only, and without `exit`, which would
# close the window when this runs through `irm | iex`.

& {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"     # Invoke-WebRequest is very slow with its progress bar
    # GitHub needs TLS 1.2, which older Windows PowerShell doesn't always switch on by itself.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $Repo = "Vitex-Lucens/latex_AI_copilot"
    $Ref = if ($env:LATEX_COPILOT_REF) { $env:LATEX_COPILOT_REF } else { "main" }
    $Prefix = Join-Path $env:LOCALAPPDATA "latex-copilot"
    $AppDir = Join-Path $Prefix "app"
    $BinDir = Join-Path $Prefix "bin"
    $Launcher = Join-Path $BinDir "latex-copilot.cmd"
    $ClaudeBin = Join-Path $env:USERPROFILE ".local\bin"
    $Uninstall = ($args -contains "-Uninstall") -or ($env:LATEX_COPILOT_UNINSTALL -eq "1")
    $AssumeYes = ($args -contains "-Yes") -or ($env:LATEX_COPILOT_YES -eq "1")
    $SkipPrereqs = ($args -contains "-SkipPrereqs") -or ($env:LATEX_COPILOT_SKIP_PREREQS -eq "1")

    function Say([string]$text) { Write-Host "  $text" }
    function Bold([string]$text) { Write-Host $text -ForegroundColor Cyan }
    function Warn([string]$text) { Write-Host $text -ForegroundColor Yellow }
    function Has([string]$name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

    function Ask([string]$question) {
        if ($AssumeYes) { return $true }
        try { $answer = Read-Host "$question [Y/n]" } catch { return $false }
        return -not ($answer -match '^\s*[nN]')
    }

    # Run a program and return its exit code and output. Windows PowerShell 5.1 turns anything a program
    # writes to stderr into an error, so that mustn't stop the script here.
    function Run-Quiet([string]$exe, [string[]]$arguments) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $out = & $exe @arguments 2>&1 | ForEach-Object { "$_" }
            return @($LASTEXITCODE, ($out -join "`n"))
        } catch {
            return @(-1, "$_")
        } finally {
            $ErrorActionPreference = $saved
        }
    }

    # Programs installed a moment ago are only on the PATH of new terminals: reload it from the registry.
    function Refresh-Path {
        $parts = @([Environment]::GetEnvironmentVariable("Path", "Machine"),
                   [Environment]::GetEnvironmentVariable("Path", "User"), $ClaudeBin, $BinDir) | Where-Object { $_ }
        $env:Path = ($parts -join ";")
    }

    function Add-UserPath([string]$dir) {
        $user = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not $user) { $user = "" }
        $entries = @($user -split ";" | Where-Object { $_ })
        if ($entries -notcontains $dir) {
            [Environment]::SetEnvironmentVariable("Path", (($entries + @($dir)) -join ";"), "User")
        }
        if (($env:Path -split ";") -notcontains $dir) { $env:Path = "$env:Path;$dir" }
    }

    function Remove-UserPath([string]$dir) {
        $user = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not $user) { return }
        $entries = @($user -split ";" | Where-Object { $_ -and ($_ -ne $dir) })
        [Environment]::SetEnvironmentVariable("Path", ($entries -join ";"), "User")
    }

    # Python 3.10 or newer: returns @{ Exe = full path; Launch = how the launcher should start it }.
    function Find-Python {
        foreach ($cand in @("py|-3.13", "py|-3.12", "py|-3.11", "py|-3.10", "python|", "python3|")) {
            $exe, $flag = $cand -split "\|", 2
            if (-not (Has $exe)) { continue }
            $arguments = @()
            if ($flag) { $arguments += $flag }
            $arguments += @("-c", "import sys; print(sys.executable) if sys.version_info >= (3, 10) else sys.exit(1)")
            $code, $out = Run-Quiet $exe $arguments
            # (The Microsoft Store's "python" placeholder fails here, which is what we want.)
            if ($code -eq 0 -and $out -and (Test-Path ($out.Trim()))) {
                if ($flag) { $launch = "py $flag" } else { $launch = '"' + $out.Trim() + '"' }
                return @{ Exe = $out.Trim(); Launch = $launch }
            }
        }
        return $null
    }

    function Latexmk-Works {
        if (-not (Has "latexmk")) { return $false }
        $code, $out = Run-Quiet "latexmk" @("--version")
        return ($code -eq 0 -and $out -match "Latexmk")
    }

    function Ghostscript-Found {
        if ((Has "gswin64c") -or (Has "gs")) { return $true }
        foreach ($base in (@($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ })) {
            if (Get-ChildItem (Join-Path $base "gs\gs*\bin\gswin*c.exe") -ErrorAction SilentlyContinue) { return $true }
        }
        return $false
    }

    function Claude-Found { (Has "claude") -or (Test-Path (Join-Path $ClaudeBin "claude.exe")) }

    # Ghostscript isn't in winget: use the official installer from Artifex's GitHub releases, silently.
    function Install-Ghostscript {
        try {
            $release = Invoke-RestMethod -UseBasicParsing -Uri "https://api.github.com/repos/ArtifexSoftware/ghostpdl-downloads/releases/latest"
            $asset = $release.assets | Where-Object { $_.name -like "*w64.exe" } | Select-Object -First 1
            if (-not $asset) { throw "the latest release has no Windows installer" }
            $exe = Join-Path ([IO.Path]::GetTempPath()) $asset.name
            Say "Downloading $($asset.name) from Artifex, Ghostscript's makers..."
            Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $exe
            # It runs with admin rights, so it must carry Artifex's valid code signature.
            $sig = Get-AuthenticodeSignature -FilePath $exe
            if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Artifex Software") {
                Remove-Item $exe -ErrorAction SilentlyContinue
                throw "the download isn't signed by Artifex Software (signature: $($sig.Status)), so it wasn't run"
            }
            Say "Installing it (Windows asks for permission)..."
            Start-Process -FilePath $exe -ArgumentList "/S" -Verb RunAs -Wait
            Remove-Item $exe -ErrorAction SilentlyContinue
        } catch {
            Warn "Couldn't install Ghostscript ($_). Get it from https://ghostscript.com/releases/gsdnld.html, then run this again."
        }
    }

    function Winget-Install([string]$id, [string]$what) {
        if (-not (Has "winget")) {
            Warn "winget (Windows' package manager) isn't available here, so install $what yourself (see the README)."
            return
        }
        Say "winget install --id $id"
        $saved = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & winget install --id $id --exact --accept-package-agreements --accept-source-agreements
        $code = $LASTEXITCODE
        $ErrorActionPreference = $saved
        if ($code -ne 0) { Warn "winget couldn't install $what (code $code). You can install it yourself and run this again." }
        Refresh-Path
    }

    # ------------------------------------------------------------------ uninstall
    if ($Uninstall) {
        if (Test-Path $Prefix) { Remove-Item -Recurse -Force $Prefix }
        Remove-UserPath $BinDir
        Write-Host "latex-copilot removed. (Its logs and deleted-file backups are in $env:USERPROFILE\.cache\latex-copilot if you want those gone too.)"
        return
    }

    Bold "Latex-Copilot installer (Windows)"
    Refresh-Path

    # ------------------------------------------------------------------ the app's files
    $Src = $null
    if ($env:LATEX_COPILOT_SOURCE -and (Test-Path (Join-Path $env:LATEX_COPILOT_SOURCE "latex_copilot\app.py"))) {
        $Src = (Resolve-Path $env:LATEX_COPILOT_SOURCE).Path
    } elseif ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "latex_copilot\app.py"))) {
        $Src = $PSScriptRoot
    } else {
        $url = if ($env:LATEX_COPILOT_SOURCE) { $env:LATEX_COPILOT_SOURCE } else { "https://github.com/$Repo/archive/$Ref.zip" }
        Bold "Downloading Latex-Copilot..."
        $tmp = Join-Path ([IO.Path]::GetTempPath()) ("latex-copilot-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmp | Out-Null
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile (Join-Path $tmp "source.zip")
        } catch {
            throw "Couldn't download $url. Check your internet connection. (If the repository is private: git clone it, then double-click install.cmd in that folder.)"
        }
        Expand-Archive -Path (Join-Path $tmp "source.zip") -DestinationPath $tmp
        $Src = Get-ChildItem $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName "latex_copilot\app.py") } |
               Select-Object -First 1 -ExpandProperty FullName
        if (-not $Src) { throw "The download doesn't contain the app." }
    }

    # ------------------------------------------------------------------ what it needs
    if (-not $SkipPrereqs) {
        Bold "Checking what Latex-Copilot needs..."
        if (-not (Find-Python)) {
            if (Ask "Python 3.10 or newer is needed. Install Python 3.12 with winget?") { Winget-Install "Python.Python.3.12" "Python" }
        }
        if (-not (Has "latexmk")) {
            Say "TeX isn't installed. MiKTeX is a TeX for Windows that fetches extra packages when a document needs them."
            if (Ask "Install MiKTeX with winget?") { Winget-Install "MiKTeX.MiKTeX" "MiKTeX" }
        }
        if (Has "initexmf") {
            # MiKTeX: install missing packages automatically, instead of asking in a pop-up the app can't show.
            $null = Run-Quiet "initexmf" @("--set-config-value=[MPM]AutoInstall=1")
        }
        if ((Has "latexmk") -and -not (Latexmk-Works) -and -not (Has "perl")) {
            Say "latexmk (which compiles documents) needs Perl."
            if (Ask "Install Strawberry Perl with winget?") { Winget-Install "StrawberryPerl.StrawberryPerl" "Strawberry Perl" }
        }
        if (-not (Ghostscript-Found)) {
            if (Ask "Install Ghostscript (needed to import PDFs) from ghostscript.com?") { Install-Ghostscript }
        }
        if (-not (Claude-Found) -and $env:LATEX_COPILOT_NO_CLAUDE -ne "1") {
            Say "Claude Code is what does the work in Latex-Copilot; it uses your own Claude account."
            if (Ask "Install Claude Code with Anthropic's official installer?") {
                & powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
                Add-UserPath $ClaudeBin           # the official installer doesn't add it to PATH itself
            }
        }
    }

    # ------------------------------------------------------------------ sign in to Claude
    # Now, while there's a console (Latex-Copilot can also do it later from its chat panel).
    $claudeExe = $null
    $found = Get-Command claude -ErrorAction SilentlyContinue
    if ($found) { $claudeExe = $found.Source } elseif (Test-Path (Join-Path $ClaudeBin "claude.exe")) { $claudeExe = Join-Path $ClaudeBin "claude.exe" }
    if ($claudeExe -and $env:CI -ne "true") {
        $code, $out = Run-Quiet $claudeExe @("auth", "status")
        if ($code -ne 0) {
            Say "Latex-Copilot uses your Claude account (Pro, Max, Team or Enterprise)."
            if (Ask "Sign in to Claude now? It opens your browser.") {
                $saved = $ErrorActionPreference
                $ErrorActionPreference = "Continue"
                & $claudeExe auth login
                $ErrorActionPreference = $saved
            }
        }
    }

    $py = Find-Python
    if (-not $py) {
        throw "Python 3.10 or newer is needed. Install it (winget install Python.Python.3.12, or from python.org), then open a new terminal and run this again."
    }

    # ------------------------------------------------------------------ the app
    Bold "Installing Latex-Copilot..."
    $new = "$AppDir.new"
    if (Test-Path $new) { Remove-Item -Recurse -Force $new }
    New-Item -ItemType Directory -Path $new | Out-Null
    Copy-Item -Recurse (Join-Path $Src "latex_copilot") $new
    foreach ($f in @("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "install.ps1", "install.cmd")) {
        $p = Join-Path $Src $f
        if (Test-Path $p) { Copy-Item $p $new }
    }
    Get-ChildItem $new -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
    Move-Item $new $AppDir
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

    # The launcher names no user folders literally (%LOCALAPPDATA%), so it works whatever the user name is.
    $run = $py.Launch
    if ($run.StartsWith('"' + $env:LOCALAPPDATA, [StringComparison]::OrdinalIgnoreCase)) {
        $run = '"%LOCALAPPDATA%' + $run.Substring($env:LOCALAPPDATA.Length + 1)
    }
    $lines = @(
        "@echo off",
        "rem Latex-Copilot launcher (created by install.ps1)",
        "setlocal",
        'set "PYTHONPATH=%LOCALAPPDATA%\latex-copilot\app;%PYTHONPATH%"',
        "$run -X utf8 -m latex_copilot %*"
    )
    Set-Content -Path $Launcher -Value $lines -Encoding ASCII
    Add-UserPath $BinDir
    # Latex-Copilot used to be called tex-annotator: remove that version (its logs and deleted-file backups stay).
    $old = Join-Path $env:LOCALAPPDATA "tex-annotator"
    if (Test-Path $old) {
        Remove-Item -Recurse -Force $old
        Remove-UserPath (Join-Path $old "bin")
        Say "Removed the previous version (tex-annotator)."
    }
    Say "app:     $AppDir"
    Say "command: $Launcher  (using $($py.Exe))"
    Write-Host ""

    Bold "Checking the setup..."
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $Launcher --check
    $checked = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $saved
    Write-Host ""
    Bold "Done. Open a new terminal (so it finds the latex-copilot command), then run:"
    Say "cd path\to\your\latex\project"
    Say "latex-copilot"
    Say "or, to rebuild a PDF as LaTeX:  latex-copilot path\to\file.pdf"
    if (-not $checked) {
        Say "Something above is still missing. Run this installer again, or install it yourself (see the README)."
        Say "Not signed in to Claude yet? Latex-Copilot's chat panel has a Sign in button."
    }
} @args
