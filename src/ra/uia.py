"""
Ra UI Automation (Windows UIA)
===============================
An accessibility layer over Win32 UI Automation so Ra can find and click
real controls by NAME - "the green button beside the loser song card" becomes
a search for the song row's text element, whose bounding rectangle pinpoints
the row; small unnamed icons next to it are then hit via the vision zoom.

UIAutomation is queried through a temp PowerShell script (dpi-context stays
inside the child, `CREATE_NO_WINDOW` so no terminal flashes). The child sets
its own Per-Monitor DPI awareness up-front so `BoundingRectangle` (physical
pixels) and `SetCursorPos` (physical) agree.

Three tools:
  ui_find(name)   -> list accessible controls (window / menu / buttons / list
                     items / rows) with their exact rectangles.
  ui_click(name)  -> click the named control (Invoke pattern, else selection,
                     else center-click; optional double-click for 'play').
  ui_type(name,text) -> click the field and set its text.
"""
import os
import subprocess
import tempfile
import typing

from ra import computer

_PS_HEADER = r"""
param(
    [string]$Action = 'list',
    [string]$Find = '',
    [string]$Region = '',
    [string]$Text = '',
    [switch]$Double
)
trap { Write-Output ("PS_ERR=" + $_.Exception.Message); exit 1 }
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class RaUiaDP { [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int v); }' -ErrorAction Stop
[void][RaUiaDP]::SetProcessDpiAwareness(2)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms

Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class RaUiaMouse { [DllImport("user32.dll")] public static extern bool SetCursorPos(int X,int Y); [DllImport("user32.dll")] public static extern void mouse_event(uint flags,uint dx,uint dy,uint data,System.IntPtr extra); }' -ErrorAction SilentlyContinue

Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;public class RaUiaWin { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); }' -ErrorAction SilentlyContinue

function Mite($name) { $name -replace '[|]','/-' }
"""

_PS_ENUM = r"""
function Enumerate {
    param([string]$Needle, [string]$Region)
    $rx1=0;$ry1=0;$rx2=0;$ry2=0
    if ($Region -ne '') {
        $parts = $Region.Split(',')
        if ($parts.Count -ge 4) { $rx1=[int]$parts[0]; $ry1=[int]$parts[1]; $rx2=[int]$parts[2]; $ry2=[int]$parts[3] }
    }
    $script:Matches = @()
    # Start from the FOREGROUND window: a far smaller tree than the whole
    # desktop (RootElement.Descendants walks EVERY open window - seconds of
    # lag) and always the window the user is actually looking at.
    $start = $null
    try {
        $fg = [RaUiaWin]::GetForegroundWindow()
        if ($fg -ne [IntPtr]::Zero) { $start = [System.Windows.Automation.AutomationElement]::FromHandle($fg) }
    } catch {}
    if ($null -eq $start) {
        try { $start = [System.Windows.Automation.AutomationElement]::FocusedElement } catch {}
    }
    if ($null -eq $start) { $start = [System.Windows.Automation.AutomationElement]::RootElement }
    try {
        $all = $start.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                              [System.Windows.Automation.Condition]::TrueCondition)
    } catch {
        $all = $null
    }
    if ($null -eq $all -or $all.Count -eq 0) { $start = [System.Windows.Automation.AutomationElement]::RootElement; $all = $start.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition) }
    $out = @()
    for ($i=0; $i -lt $all.Count; $i++) {
        $e = $all.Item($i)
        try { $nm = $e.Current.Name } catch { $nm = '' }
        if ([string]::IsNullOrWhiteSpace($nm)) { continue }
        $nm = $nm.Trim()
        if ($Needle -ne '' -and $nm -notlike "*$Needle*") { continue }
        $r = $e.Current.BoundingRectangle
        if ($r.Width -le 3 -or $r.Height -le 3) { continue }
        if ($rx2 -gt $rx1 -and ($r.X -lt $rx1 -or $r.Y -lt $ry1 -or ($r.X+$r.Width) -gt $rx2 -or ($r.Y+$r.Height) -gt $ry2)) { continue }
        $ct = ($e.Current.ControlType.ProgrammaticName -replace 'ControlType\.','')
        $x=[int][math]::Round($r.X); $y=[int][math]::Round($r.Y); $w=[int][math]::Round($r.Width); $h=[int][math]::Round($r.Height)
        $dupe = $false
        foreach ($already in $out) {
            if ($already.Ct -eq $ct -and $already.Nm -eq $nm -and
                $x -ge $already.X -and $y -ge $already.Y -and
                ($x+$w) -le ($already.X+$already.W) -and ($y+$h) -le ($already.Y+$already.H)) { $dupe = $true; break }
        }
        if ($dupe) { continue }
        # Cache the live element so click/type never re-enumerate the tree.
        $out += [PSCustomObject]@{ Ct=$ct; Nm=$nm; X=$x; Y=$y; W=$w; H=$h; Idx=$script:Matches.Count }
        $script:Matches += $e
    }
    $out = $out | Sort-Object H, X, Y
    if ($out.Count -gt 24) { $out = $out | Select-Object -First 24 }
    foreach ($o in $out) {
        Write-Output $o
    }
}
"""

_PS_CLICK = r"""
function TryInvoke {
    param($e)
    $invoke = $null
    try { $invoke = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern) } catch {}
    if ($invoke) { try { $invoke.Invoke(); return 'invoke' } catch {} }
    $sel = $null
    try { $sel = $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern) } catch {}
    if ($sel) { try { $sel.Select(); return 'select' } catch {} }
    # Fall back to a real click at the element center (physical coords,
    # same DPI context as BoundingRectangle).
    $r = $e.Current.BoundingRectangle
    $cx = [int][math]::Round($r.X + $r.Width/2)
    $cy = [int][math]::Round($r.Y + $r.Height/2)
    [RaUiaMouse]::SetCursorPos($cx, $cy)
    Start-Sleep -Milliseconds 60
    if ($script:Double) {
        [RaUiaMouse]::mouse_event(0x2,0,0,0,[IntPtr]::Zero); [RaUiaMouse]::mouse_event(0x4,0,0,0,[IntPtr]::Zero)
        Start-Sleep -Milliseconds 80
        [RaUiaMouse]::mouse_event(0x2,0,0,0,[IntPtr]::Zero); [RaUiaMouse]::mouse_event(0x4,0,0,0,[IntPtr]::Zero)
        return 'dblclick'
    }
    [RaUiaMouse]::mouse_event(0x2,0,0,0,[IntPtr]::Zero); [RaUiaMouse]::mouse_event(0x4,0,0,0,[IntPtr]::Zero)
    return 'click'
}
"""

_FUNCS = _PS_HEADER + _PS_ENUM + _PS_CLICK + r"""

$hits = @(Enumerate -Needle $Find -Region $Region)
if ($hits.Count -eq 0) { Write-Output 'NO_MATCH'; exit 0 }

if ($Action -eq 'find') {
    foreach ($m in $hits) {
        if ($Find -ne '' -or $m.Nm -match '\w') { Write-Output ("{0}|{1}|{2}|{3}|{4}|{5}" -f $m.Ct, (Mite $m.Nm), $m.X, $m.Y, $m.W, $m.H) }
    }
    exit 0
}

# pick the smallest (most specific) matching control - a row's text beats its
# giant container; among equal sizes keep the first (top-left).
$best = $hits[0]
foreach ($m in $hits) {
    if (($m.W * $m.H) -lt ($best.W * $best.H)) { $best = $m }
}
$bestElem = $null
try { if ($best.Idx -lt $script:Matches.Count) { $bestElem = $script:Matches[$best.Idx] } } catch {}

if ($Action -eq 'click') {
    $way = 'click'
    if ($bestElem) { $way = TryInvoke $bestElem }
    else {
        $bx = $best.X + [int][math]::Round($best.W/2)
        $by = $best.Y + [int][math]::Round($best.H/2)
        [RaUiaMouse]::SetCursorPos($bx, $by); Start-Sleep -Milliseconds 60
        if ($Double) {
            [RaUiaMouse]::mouse_event(0x2,0,0,0,[IntPtr]::Zero); [RaUiaMouse]::mouse_event(0x4,0,0,0,[IntPtr]::Zero)
            Start-Sleep -Milliseconds 80
            [RaUiaMouse]::mouse_event(0x2,0,0,0,[IntPtr]::Zero); [RaUiaMouse]::mouse_event(0x4,0,0,0,[IntPtr]::Zero)
        } else {
            [RaUiaMouse]::mouse_event(0x2,0,0,0,[IntPtr]::Zero); [RaUiaMouse]::mouse_event(0x4,0,0,0,[IntPtr]::Zero)
        }
    }
    Write-Output ("OK|{0}|{1}|{2}|{3}|{4}|{5}|{6}" -f $best.Ct, (Mite $best.Nm), $best.X, $best.Y, $best.W, $best.H, $way)
    exit 0
}
if ($Action -eq 'type') {
    # Fast path: set the value directly on the matched field element.
    $usedValue = $false
    if ($bestElem) {
        try {
            $vp = $bestElem.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $vp.SetValue($script:Text)
            $usedValue = $true
        } catch {}
    }
    if (-not $usedValue) {
        # Fallback: click the field, then paste via the clipboard.
        $cx = $best.X + [int][math]::Round($best.W/2)
        $cy = $best.Y + [int][math]::Round($best.H/2)
        [RaUiaMouse]::SetCursorPos($cx, $cy); Start-Sleep -Milliseconds 60
        [RaUiaMouse]::mouse_event(0x2,0,0,0,[IntPtr]::Zero); [RaUiaMouse]::mouse_event(0x4,0,0,0,[IntPtr]::Zero)
        Start-Sleep -Milliseconds 150
        [System.Windows.Forms.Clipboard]::SetText([string]$script:Text)
        Start-Sleep -Milliseconds 80
        [System.Windows.Forms.SendKeys]::SendWait('^v')
    }
    Write-Output ("TYPED|{0}|{1}|{2}" -f $best.Ct, (Mite $best.Nm), [string]$script:Text)
    exit 0
}
Write-Output 'NO_MATCH'
exit 0
"""


_PS_FILE: typing.Optional[str] = None


def _script_file() -> str:
    """Write the PowerShell helper ONCE per process and reuse it - a fresh
    mkstemp+write on every call added avoidable latency to every ui_* call."""
    global _PS_FILE
    if _PS_FILE is None or not os.path.exists(_PS_FILE):
        fd, path = tempfile.mkstemp(suffix=".ps1", prefix="ra_uia_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_FUNCS)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        _PS_FILE = path
    return _PS_FILE


def _run(action: str, find: str = "", region: str = "",
         text: str = "", double: bool = False) -> str:
    """Run the UIA script; returns stripped stdout (or error text).
    NOTE: powershell.exe DROPS empty-string command-line arguments, so empty
    params are simply omitted - the script's defaults already mean 'any'."""
    path = _script_file()
    try:
        run_args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path,
                    "-Action", action]
        if find:
            run_args += ["-Find", find]
        if region:
            run_args += ["-Region", region]
        if text:
            run_args += ["-Text", text]
        if double:
            run_args.append("-Double")
        proc = subprocess.run(
            ["powershell"] + run_args,
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace", **_no_window(),
        )
    except subprocess.TimeoutExpired:
        return "Timeout while reading the app's UI tree."
    except Exception as e:
        return f"UI automation failed: {e}"
    stdout = (proc.stdout or "").strip()
    if stdout.startswith("PS_ERR="):
        return stdout[len("PS_ERR="):]
    if proc.returncode != 0 and not stdout:
        return f"UI automation error (exit {proc.returncode})."
    return stdout


def ui_find(name: str = "", region: str = "") -> str:
    """Read the accessible UI tree of the active window. `name` narrows to
    controls whose text CONTAINS it (a song title, a menu label). Returns
    lines `CONTROLTYPE|TEXT|X|Y|W|H` in PHYSICAL screen pixels."""
    out = _run("find", find=name, region=region)
    if out == "NO_MATCH" or not out:
        return "No control found" + (f" for '{name}'." if name else ".")
    return out


def ui_click(name: str, double: bool = False, region: str = "") -> str:
    """Find the control whose text contains `name` and click it. Double-click
    when the user wants to 'open' or 'play' content (e.g. a track). Won't type
    into a field."""    
    if not name.strip():
        return "ui_click needs a text name to look for."
    line = _run("click", find=name, region=region, double=double)
    if line == "NO_MATCH" or not line:
        return f"Couldn't find a visible control with text '{name}'."
    parts = line.split("|")
    kind = parts[1] if len(parts) > 1 else ""
    return (f"{('Double-clicked' if double else 'Clicked')} {kind} "
            f"'{parts[2]}'")


def ui_type(name: str, text: str) -> str:
    """Find the field whose text contains `name`, click it, and put `text`
    into it (value pattern; falls back to paste)."""
    if not name.strip():
        return "ui_type needs a field name."
    line = _run("type", find=name, text=text)
    if line == "NO_MATCH" or not line:
        return f"Couldn't find a field with text '{name}'."
    return f"Typed into '{name}'."


def _no_window() -> dict:
    return computer._no_window()