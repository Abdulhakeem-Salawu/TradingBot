' Runs a command with no console window and returns its exit code.
' Task Scheduler would otherwise flash a black window on screen at every run.
'   wscript.exe hidden.vbs "C:\path\run_executor.cmd" ".env" "demo"
Set shell = CreateObject("WScript.Shell")
cmd = ""
For Each arg In WScript.Arguments
    cmd = cmd & " """ & arg & """"
Next
WScript.Quit shell.Run(Trim(cmd), 0, True)
