# Creates the azd environment and seeds the infra parameters simplechat's main.bicep expects.
# Run after `az login` and `azd auth login` - azd env new validates the subscription.
#
# main.parameters.json sources most parameters from azd environment variables, and azd persists
# prompted ones into .azure/<env>/config.json. Seeding both means `azd up` asks nothing.

param(
    [Parameter(Mandatory)][string]$EnvName,
    [Parameter(Mandatory)][string]$SubscriptionId,
    [Parameter(Mandatory)][string]$Location,
    [string]$Dest = 'C:\simplechat',
    [string]$AppName = 'simplechat',
    [string]$AuthenticationType = 'key',
    [string]$RedisAuthenticationType = 'key',
    [ValidateSet('true', 'false')][string]$ConfigureApplicationPermissions = 'true',
    [ValidateSet('true', 'false')][string]$DeployContentSafety = 'true',
    [ValidateSet('true', 'false')][string]$DeployRedisCache = 'true',
    [ValidateSet('true', 'false')][string]$DeploySpeechService = 'true',
    [ValidateSet('true', 'false')][string]$DeployVideoIndexerService = 'false',
    [ValidateSet('true', 'false')][string]$EnableDiagLogging = 'true',
    [ValidateSet('true', 'false')][string]$EnablePrivateNetworking = 'false',
    [string]$OpenAIDeploymentType = 'GlobalStandard'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\guest.ps1')
. (Join-Path $PSScriptRoot 'lib\install-helpers.ps1')
Update-SessionPath

$deployers = Join-Path $Dest 'deployers'
Set-Location -LiteralPath $deployers

Write-Host "[1/4] Selecting subscription" -ForegroundColor Cyan
& az.cmd account set --subscription $SubscriptionId
if ($LASTEXITCODE -ne 0) { throw "az account set failed with $LASTEXITCODE" }
& az.cmd account show --query '{name:name, id:id, tenant:tenantId}' -o table

Write-Host ""
Write-Host "[2/4] Creating azd environment '$EnvName'" -ForegroundColor Cyan
$existing = & azd.exe env list --output json 2>$null | ConvertFrom-Json
if ($existing -and ($existing | Where-Object { $_.Name -eq $EnvName })) {
    Write-Host "environment already exists, selecting it"
    & azd.exe env select $EnvName
} else {
    & azd.exe env new $EnvName --subscription $SubscriptionId --location $Location
    if ($LASTEXITCODE -ne 0) { throw "azd env new failed with $LASTEXITCODE" }
}

Write-Host ""
Write-Host "[3/4] Seeding infra parameters" -ForegroundColor Cyan
$parameters = [ordered]@{
    appName                         = $AppName
    authenticationType              = $AuthenticationType
    configureApplicationPermissions = [bool]::Parse($ConfigureApplicationPermissions)
    deployContentSafety             = [bool]::Parse($DeployContentSafety)
    deployRedisCache                = [bool]::Parse($DeployRedisCache)
    deploySpeechService             = [bool]::Parse($DeploySpeechService)
    deployVideoIndexerService       = [bool]::Parse($DeployVideoIndexerService)
    enableDiagLogging               = [bool]::Parse($EnableDiagLogging)
    enablePrivateNetworking         = [bool]::Parse($EnablePrivateNetworking)
    environment                     = $EnvName
    openAIDeploymentType            = $OpenAIDeploymentType
}

# main.parameters.json reads these from the azd environment.
$envVars = [ordered]@{
    'environment'                       = $EnvName
    'env_DEPLOYMENT_APPNAME'            = $AppName
    'AUTHENTICATION_TYPE'               = $AuthenticationType
    'REDIS_AUTHENTICATION_TYPE'         = $RedisAuthenticationType
    'CONFIGURE_APPLICATION_PERMISSIONS' = $ConfigureApplicationPermissions
    'DEPLOY_CONTENT_SAFETY'             = $DeployContentSafety
    'DEPLOY_REDIS_CACHE'                = $DeployRedisCache
    'DEPLOY_SPEECH_SERVICE'             = $DeploySpeechService
    'DEPLOY_VIDEO_INDEXER_SERVICE'      = $DeployVideoIndexerService
    'ENABLE_DIAG_LOGGING'               = $EnableDiagLogging
    'ENABLE_PRIVATE_NETWORKING'         = $EnablePrivateNetworking
    'OPENAI_DEPLOYMENT_TYPE'            = $OpenAIDeploymentType
    'REDIS_LOCATION'                    = $Location
}
foreach ($name in $envVars.Keys) {
    & azd.exe env set --environment $EnvName $name $envVars[$name] | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "azd env set $name failed with $LASTEXITCODE" }
    Write-Host ("  {0,-35} {1}" -f $name, $envVars[$name])
}

$configPath = Join-Path $deployers ".azure\$EnvName\config.json"
$config = if (Test-Path -LiteralPath $configPath) {
    Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
} else {
    [pscustomobject]@{}
}
$config | Add-Member -NotePropertyName 'infra' -NotePropertyValue ([pscustomobject]@{
    parameters = [pscustomobject]$parameters
}) -Force
Write-JsonAtomic -Path $configPath -Value $config
Write-Host "  wrote $configPath"

Write-Host ""
Write-Host "[4/4] Environment summary" -ForegroundColor Cyan
# Prove azd can actually parse what we wrote before claiming the environment is ready.
$values = & azd.exe env get-values --environment $EnvName 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "azd cannot read the environment it was just given: $values"
}
$values | Select-String -NotMatch 'SECRET|PASSWORD|KEY='

mark 'azd_env_ready' -Detail "$EnvName in $Location"
exit 0
