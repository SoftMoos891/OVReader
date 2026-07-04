# Start de busvervoer-monitor voor de provincie Utrecht.
# Dashboard is daarna te bereiken op http://127.0.0.1:5151
Set-Location $PSScriptRoot
& "./venv/Scripts/python.exe" -m app.server
