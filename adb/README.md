# adb/

这个目录用来放 Android platform-tools，**二进制不入库**（体积大，且版权归 Google，随 MIT 分发不合适）。

需要以下两个文件：

```
adb/adb.exe
adb/AdbWinApi.dll
```

获取方式：从 [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) 下载 Windows 版压缩包，解压后把这两个文件拷进来即可。

程序找 adb 的顺序：

1. 本目录（`adb/adb.exe`）—— 打包后的 exe 用这个
2. 可执行文件同级目录
3. `~/platform-tools/adb.exe`、`C:\platform-tools\adb.exe`、`%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`
4. `PATH` 里的 `adb`

都没有也能跑：只是「ADB 通道」和「安装结果核验」不可用，局域网直连推送不受影响。
