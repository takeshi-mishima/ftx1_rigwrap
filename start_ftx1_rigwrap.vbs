' start_ftx1_rigwrap.vbs : start ftx1_rigwrap.py without a console window
'
' rigctld for the FTX-1 may be started before or after this (the wrapper connects to rigctld
' when a client such as WSJT-X connects).
'
' (c) 2026 Takeshi Mishima JK1VUZ

Set shell = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
currentDir = fso.GetParentFolderName(WScript.ScriptFullName)

' --- Setting ---
' Options, e.g. "--listen 4535:main --listen 4536:sub --debug" (leave empty for defaults)
'   defaults: --rigctld 127.0.0.1:4534 --listen 4535:main --listen 4536:sub
'   MODE of --listen PORT:MODE : follow   = side selected on the panel (VS)
'                                opposite = the other side
'                                main     = always MAIN
'                                sub      = always SUB
EXTRA_OPTS = ""

' pythonw.exe must be in PATH (python.org installer "Add python.exe to PATH")
cmd = "pythonw """ & currentDir & "\ftx1_rigwrap.py"" " & EXTRA_OPTS

shell.CurrentDirectory = currentDir
shell.Run cmd, 0, False
