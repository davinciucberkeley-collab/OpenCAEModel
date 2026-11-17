@echo off
rem Open the K09-L design-review website (static page: needs no Python and no server).
rem Rebuild it from the Design Monitor (Build website) or: python -m opencae.web.build
set "SITE=%~dp0opencae\examples\k09l\website\index.html"
if exist "%USERPROFILE%\.opencae\k09l_website\index.html" set "SITE=%USERPROFILE%\.opencae\k09l_website\index.html"
start "" "%SITE%"
