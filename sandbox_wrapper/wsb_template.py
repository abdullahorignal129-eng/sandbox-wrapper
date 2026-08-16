# wsb_template.py - Windows Sandbox configuration XML template
WSB_TEMPLATE = '''\
<Configuration>
  <Networking>Enable</Networking>
  <MappedFolder>
    <HostFolder>{host_folder}</HostFolder>
    <SandboxFolder>{sandbox_folder}</SandboxFolder>
    <ReadOnly>false</ReadOnly>
  </MappedFolder>
  <LogonCommand>
    <Command>cmd /c python {sandbox_folder}\\server.py</Command>
  </LogonCommand>
</Configuration>
'''
