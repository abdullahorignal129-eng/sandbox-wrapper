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
    <Command>cmd /c python C:/Users/WDAGUtilityAccount/Desktop/Shared/server.py</Command>
  </LogonCommand>
</Configuration>
'''
