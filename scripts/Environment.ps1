$ErrorActionPreference = 'Continue'
Get-CimInstance Win32_OperatingSystem | Format-List Caption,OSArchitecture,Version
Get-CimInstance Win32_Processor | Format-List Name,Architecture
Get-CimInstance Win32_ComputerSystem | Format-List Manufacturer,Model,TotalPhysicalMemory
Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'" | Select-Object DeviceID,Size,FreeSpace | Format-List
Get-ItemProperty 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0' | Select-Object ProcessorNameString | Format-List
