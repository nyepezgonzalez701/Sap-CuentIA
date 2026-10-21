# Carga las variables de .env.qa en la sesion actual de PowerShell.
# Uso (dentro de la carpeta del proyecto):
#     . .\usar_entorno_qa.ps1
# El punto inicial es importante: sin el, las variables no quedan en tu sesion.

Get-Content ".env.qa" | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        $nombre = $matches[1].Trim()
        $valor = $matches[2].Trim()
        [System.Environment]::SetEnvironmentVariable($nombre, $valor)
        if ($nombre -eq "DATABASE_URL") {
            Write-Host "DATABASE_URL activo -> $valor"
        } else {
            Write-Host "$nombre cargado."
        }
    }
}
