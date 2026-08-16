# wsb_template.py
WSB_TEMPLATE = '''\
<Configuration>
  <Networking>Enable</Networking>
  
  <!-- Shared folder (writable, for communication) -->
  <MappedFolder>
    <HostFolder>{host_folder}</HostFolder>
    <SandboxFolder>C:/Users/WDAGUtilityAccount/Desktop/Shared</SandboxFolder>
    <ReadOnly>false</ReadOnly>
  </MappedFolder>
  
  <!-- Python 3.11 (read-only) -->
  <MappedFolder>
    <HostFolder>F:/Apps/Dev/Python/311</HostFolder>
    <SandboxFolder>C:/Python311</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>
  
  <!-- Python 3.12 (read-only) -->
  <MappedFolder>
    <HostFolder>F:/Apps/Dev/Python/312</HostFolder>
    <SandboxFolder>C:/Python312</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>
  
  <!-- Python 3.13 (read-only) -->
  <MappedFolder>
    <HostFolder>F:/Apps/Dev/Python/313</HostFolder>
    <SandboxFolder>C:/Python313</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>
  
  <!-- Python 3.14 (read-only) -->
  <MappedFolder>
    <HostFolder>F:/Apps/Dev/Python/314</HostFolder>
    <SandboxFolder>C:/Python314</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>
  
  <LogonCommand>
    <Command>cmd /c C:/Python312/python.exe C:/Users/WDAGUtilityAccount/Desktop/Shared/server.py</Command>
  </LogonCommand>
</Configuration>
'''
