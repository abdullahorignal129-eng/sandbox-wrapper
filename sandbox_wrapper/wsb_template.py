WSB_TEMPLATE = r'''<Configuration>
    <Networking>Enable</Networking>
    <MappedFolders>
        <!-- Shared folder (writable, for communication, AND holds the -->
        <!-- manually-copied Python installs + persistent/throwaway venvs) -->
        <MappedFolder>
            <HostFolder>{host_folder}</HostFolder>
            <SandboxFolder>C:\Shared</SandboxFolder>
            <ReadOnly>false</ReadOnly>
        </MappedFolder>
    </MappedFolders>

    <LogonCommand>
        <Command>cmd /c C:\Shared\Python312\python.exe -u C:\Shared\server.py &gt; C:\Shared\server.log 2&gt;&amp;1</Command>
    </LogonCommand>
</Configuration>
'''
