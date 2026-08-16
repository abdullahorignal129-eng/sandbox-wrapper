# wsb_template.py
WSB_TEMPLATE = '''\
<Configuration>
  <Networking>Enable</Networking>
  <MappedFolder>
    <HostFolder>{host_folder}</HostFolder>
    <SandboxFolder>C:/Users/WDAGUtilityAccount/Desktop/Shared</SandboxFolder>
    <ReadOnly>false</ReadOnly>
  </MappedFolder>
  <LogonCommand>
    <Command>cmd /c C:/Users/WDAGUtilityAccount/Desktop/Shared/Python_versions/312/python.exe C:/Users/WDAGUtilityAccount/Desktop/Shared/server.py</Command>
  </LogonCommand>
</Configuration>
'''
