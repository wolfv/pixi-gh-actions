@echo off
if not exist "%PREFIX%\share\gh-actions\actions\checkout" mkdir "%PREFIX%\share\gh-actions\actions\checkout"
xcopy /E /I /Y "%SRC_DIR%" "%PREFIX%\share\gh-actions\actions\checkout"

if not exist "%PREFIX%\Scripts" mkdir "%PREFIX%\Scripts"
copy "%RECIPE_DIR%\checkout.ps1" "%PREFIX%\Scripts\checkout.ps1"
copy "%RECIPE_DIR%\checkout.bat" "%PREFIX%\Scripts\checkout.bat"
