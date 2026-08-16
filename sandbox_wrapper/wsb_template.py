WSB_TEMPLATE = '''\
<Configuration>
  <Networking>Enable</Networking>

  <!-- Shared folder (writable, for communication) -->
  <MappedFolder>
    <HostFolder>{host_folder}</HostFolder>
    <SandboxFolder>C:\Shared</SandboxFolder>
    <ReadOnly>false</ReadOnly>
  </MappedFolder>

  <!-- Python 3.11 (read-only) -->
  <MappedFolder>
    <HostFolder>F:\Apps\Dev\Python\311</HostFolder>
    <SandboxFolder>C:\Python311</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>

  <!-- Python 3.12 (read-only) -->
  <MappedFolder>
    <HostFolder>F:\Apps\Dev\Python\312</HostFolder>
    <SandboxFolder>C:\Python312</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>

  <!-- Python 3.13 (read-only) -->
  <MappedFolder>
    <HostFolder>F:\Apps\Dev\Python\313</HostFolder>
    <SandboxFolder>C:\Python313</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>

  <!-- Python 3.14 (read-only) -->
  <MappedFolder>
    <HostFolder>F:\Apps\Dev\Python\314</HostFolder>
    <SandboxFolder>C:\Python314</SandboxFolder>
    <ReadOnly>true</ReadOnly>
  </MappedFolder>

  <LogonCommand>
    <Command>cmd /c C:\Python312\python.exe C:\Shared\server.py &gt; C:\Shared\server.log 2&gt;&amp;1</Command>
  </LogonCommand>
</Configuration>
'''
