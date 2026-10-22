package com.hamenot.tclpush;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.drawable.Drawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.text.Spannable;
import android.text.SpannableString;
import android.text.style.ForegroundColorSpan;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * 手机版：选一个 APK，通过电视的「多屏互动」通道推过去。
 *
 * 和 Windows 版的区别只有传输通道 —— 手机上没有 adb，所以固定走局域网直连：
 * 本机起 HTTP 服务，把内网地址报给电视，电视自己来下载。
 * 相应地也没有 adb 侧的安装结果核验，只能以电视回传为准。
 */
public class MainActivity extends Activity {

    private static final int REQ_PICK = 1001;
    private static final String SP_NAME = "tclpush";
    private static final int MAX_LOG_CHARS = 20000;

    private EditText ipInput;
    private CheckBox rememberBox;
    private TextView apkName;
    private TextView apkPkg;
    private TextView apkVer;
    private TextView pickTag;
    private TextView logText;
    private TextView statusText;
    private ImageView apkIcon;
    private Button btnPush;
    private Button btnTest;
    private ProgressBar progressBar;
    private ScrollView logScroll;

    private File apkFile;
    private String pkgName;
    private String appLabel;
    private String versionName;
    private long versionCode;

    private boolean restoringConfig;
    private volatile boolean busy;
    private PushCore.StopFlag stopFlag;
    private PowerManager.WakeLock wakeLock;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        ipInput = (EditText) findViewById(R.id.ip);
        rememberBox = (CheckBox) findViewById(R.id.remember);
        apkName = (TextView) findViewById(R.id.apk_name);
        apkPkg = (TextView) findViewById(R.id.apk_pkg);
        apkVer = (TextView) findViewById(R.id.apk_ver);
        pickTag = (TextView) findViewById(R.id.pick_tag);
        logText = (TextView) findViewById(R.id.log_text);
        statusText = (TextView) findViewById(R.id.status);
        apkIcon = (ImageView) findViewById(R.id.apk_icon);
        btnPush = (Button) findViewById(R.id.btn_push);
        btnTest = (Button) findViewById(R.id.btn_test);
        progressBar = (ProgressBar) findViewById(R.id.progress);
        logScroll = (ScrollView) findViewById(R.id.log_scroll);

        apkIcon.setImageResource(android.R.drawable.sym_def_app_icon);
        apkIcon.setAlpha(0.35f);

        findViewById(R.id.card).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                pickApk();
            }
        });

        btnPush.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onPushClick();
            }
        });

        btnTest.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                testConnection();
            }
        });

        // 勾选状态本身就是一个设置项，勾上就该落盘，
        // 不能等到点推送才写 —— 否则只勾选不推送，重启后什么都没记住
        rememberBox.setOnCheckedChangeListener(new CompoundButton.OnCheckedChangeListener() {
            @Override
            public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
                if (!restoringConfig) {
                    saveConfig(ipInput.getText().toString().trim());
                }
            }
        });

        loadConfig();
        log("选择 APK 后填上电视 IP，点「推送到电视」", "muted");
    }

    // ------------------------------------------------------------ 选文件

    private void pickApk() {
        if (busy) {
            log("正在推送，先等这次结束", "muted");
            return;
        }
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{
                "application/vnd.android.package-archive", "application/octet-stream"});
        try {
            startActivityForResult(i, REQ_PICK);
        } catch (Exception e) {
            log("打不开文件选择器：" + e.getMessage(), "err");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK || resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        loadApk(data.getData());
    }

    private void loadApk(final Uri uri) {
        log("正在读取文件…", "muted");
        final Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                File out = new File(getCacheDir(), "chosen.apk");
                long total = 0;
                InputStream in = null;
                OutputStream os = null;
                try {
                    in = getContentResolver().openInputStream(uri);
                    if (in == null) {
                        throw new IllegalStateException("读不到这个文件");
                    }
                    os = new FileOutputStream(out);
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        os.write(buf, 0, n);
                        total += n;
                    }
                    os.flush();
                } catch (Exception e) {
                    log("读取文件失败：" + e.getMessage(), "err");
                    return;
                } finally {
                    closeQuietly(in);
                    closeQuietly(os);
                }

                final long size = total;
                PackageManager pm = getPackageManager();
                PackageInfo pi = null;
                String label = null;
                Drawable icon = null;
                String vn = null;
                long vc = 0;
                try {
                    pi = pm.getPackageArchiveInfo(out.getAbsolutePath(), 0);
                    if (pi != null && pi.applicationInfo != null) {
                        ApplicationInfo ai = pi.applicationInfo;
                        ai.sourceDir = out.getAbsolutePath();
                        ai.publicSourceDir = out.getAbsolutePath();
                        label = pm.getApplicationLabel(ai).toString();
                        icon = pm.getApplicationIcon(ai);
                        vn = pi.versionName;
                        vc = Build.VERSION.SDK_INT >= 28 ? pi.getLongVersionCode() : pi.versionCode;
                    }
                } catch (Exception e) {
                    log("解析 APK 信息出错：" + e.getMessage(), "muted");
                }

                if (pi == null) {
                    log("这个文件不是有效的 APK，换一个试试", "err");
                    return;
                }

                apkFile = out;
                pkgName = pi.packageName;
                appLabel = label == null ? pi.packageName : label;
                versionName = vn == null ? "-" : vn;
                versionCode = vc;

                final String fLabel = appLabel;
                final String fVer = versionName;
                final long fVc = versionCode;
                final Drawable fIcon = icon;
                final double mb = size / 1048576.0;

                apkName.post(new Runnable() {
                    @Override
                    public void run() {
                        apkName.setText(fLabel);
                        apkPkg.setText(pkgName);
                        apkVer.setText("版本 " + fVer + "  ·  " + String.format("%.1f MB", mb));
                        pickTag.setText("重选");
                        if (fIcon != null) {
                            apkIcon.setImageDrawable(fIcon);
                            apkIcon.setAlpha(1f);
                        }
                    }
                });
                log("已载入 " + fLabel + "（" + pkgName + "，" + fVer + "）", "ok");
            }
        }, "load-apk");
        t.setDaemon(true);
        t.start();
    }

    // ------------------------------------------------------------ 推送

    private void onPushClick() {
        if (busy) {
            if (stopFlag != null) {
                stopFlag.stop();
            }
            statusText.setText("正在停止…");
            btnPush.setEnabled(false);
            return;
        }
        final String ip = ipInput.getText().toString().trim();
        if (!NetUtil.validIp(ip)) {
            log("IP 格式不对，例如 192.168.31.135", "err");
            ipInput.requestFocus();
            return;
        }
        if (apkFile == null || pkgName == null) {
            log("先选择一个 APK 文件", "err");
            return;
        }
        final String localIp = NetUtil.lanIp();
        if (NetUtil.score(localIp) < 3) {
            log("当前网络不像局域网（本机地址 " + localIp + "）", "err");
            log("请先连到和电视同一个 Wi-Fi 再试", "muted");
            return;
        }
        saveConfig(ip);
        startPush(ip, localIp);
    }

    private void startPush(final String ip, final String localIp) {
        busy = true;
        stopFlag = new PushCore.StopFlag();
        setBusyUi(true);
        keepAwake(true);

        log("开始推送到 " + ip + "（局域网直连，本机 " + localIp + "）", "");

        Thread worker = new Thread(new Runnable() {
            @Override
            public void run() {
                ApkServer srv = null;
                try {
                    if (!NetUtil.portOpen(ip, PushCore.TV_PORT, 4000)) {
                        log("连不上 " + ip + ":" + PushCore.TV_PORT, "err");
                        log("检查电视是否开机、IP 是否正确、是否和手机同一 Wi-Fi", "muted");
                        return;
                    }
                    log("电视会弹确认框：按遥控器「右」选中「同意」，再按确认", "muted");
                    srv = new ApkServer(apkFile, new ApkServer.Log() {
                        @Override
                        public void line(String text, String level) {
                            log(text, level);
                        }
                    });
                    int port = srv.start();
                    String url = "http://" + localIp + ":" + port + "/app.apk";
                    log("APK 服务：" + url, "muted");

                    PushCore.Listener listener = new PushCore.Listener() {
                        @Override
                        public void log(String line, String level) {
                            MainActivity.this.log(line, level);
                        }

                        @Override
                        public void stage(String stage) {
                            onStage(stage);
                        }
                    };

                    PushCore.Result r = PushCore.push(ip, url, pkgName, appLabel, versionCode,
                            apkFile.length(), listener, stopFlag,
                            PushCore.DEFAULT_NO_PROGRESS, PushCore.DEFAULT_HARD);
                    handleResult(r, srv);
                } catch (Exception e) {
                    log("出错：" + e.getMessage(), "err");
                } finally {
                    if (srv != null) {
                        srv.stop();
                    }
                    busy = false;
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            setBusyUi(false);
                            keepAwake(false);
                        }
                    });
                }
            }
        }, "push");
        worker.setDaemon(true);
        worker.start();
    }

    private void handleResult(PushCore.Result r, ApkServer srv) {
        if ("ok".equals(r.status)) {
            log("安装完成 ✔  " + pkgName + " " + versionName, "ok");
            statusText.setText("已安装到电视");
        } else if ("denied".equals(r.status)) {
            log("电视上选择了拒绝，已取消。", "err");
            statusText.setText("电视上拒绝了安装");
        } else if ("stopped".equals(r.status)) {
            log("已停止。", "");
            statusText.setText("");
        } else if ("timeout".equals(r.status)) {
            log("超时：" + r.message, "err");
            if (srv != null && !srv.served()) {
                log("电视没能从手机取到文件，检查路由器是否开了「AP 隔离」", "muted");
            } else {
                log("电视可能已经装好了，去应用列表确认一下。手机上没法像电脑那样用 adb 反查结果。", "muted");
            }
            statusText.setText("超时");
        } else {
            log("推送失败：" + r.message, "err");
            statusText.setText("推送失败");
        }
    }

    private void onStage(String stage) {
        String text = null;
        if ("confirm".equals(stage)) {
            text = "电视已弹窗，请按遥控器「右」选中「同意」，再按确认";
        } else if ("download".equals(stage)) {
            text = "电视正在下载 APK…";
        } else if ("install".equals(stage)) {
            text = "电视正在安装…";
        }
        if (text != null) {
            log(text, "");
            statusText.setText(text);
        }
    }

    // ------------------------------------------------------------ 测试连接

    private void testConnection() {
        final String ip = ipInput.getText().toString().trim();
        if (!NetUtil.validIp(ip)) {
            log("IP 格式不对，例如 192.168.31.135", "err");
            return;
        }
        log("正在测试 " + ip + " …", "muted");
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                boolean ok = NetUtil.portOpen(ip, PushCore.TV_PORT, 3000);
                if (ok) {
                    log("连接正常：多屏互动端口可达", "ok");
                } else {
                    log("连不上 " + ip + ":" + PushCore.TV_PORT, "err");
                    log("电视上的「多屏互动」要打开，端口才会监听", "muted");
                }
            }
        }, "probe");
        t.setDaemon(true);
        t.start();
    }

    // ------------------------------------------------------------ 界面状态

    private void setBusyUi(boolean on) {
        btnPush.setEnabled(true);
        if (on) {
            btnPush.setText(R.string.stop);
            btnPush.setBackgroundResource(R.drawable.bg_btn_stop);
            btnPush.setTextColor(getColor(R.color.err));
            progressBar.setVisibility(View.VISIBLE);
            btnTest.setEnabled(false);
            pickTag.setText("推送中");
        } else {
            btnPush.setText(R.string.push);
            btnPush.setBackgroundResource(R.drawable.bg_btn_primary);
            btnPush.setTextColor(0xffffffff);
            progressBar.setVisibility(View.GONE);
            btnTest.setEnabled(true);
            pickTag.setText(apkFile == null ? "选择" : "重选");
        }
    }

    private void keepAwake(boolean on) {
        if (on) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (wakeLock == null) {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "tclpush:push");
                wakeLock.setReferenceCounted(false);
            }
            if (!wakeLock.isHeld()) {
                wakeLock.acquire(10 * 60 * 1000L);
            }
        } else {
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        }
    }

    // ------------------------------------------------------------ 日志

    private void log(String line, String level) {
        final String text = PushCore.now() + "  " + line + "\n";
        final int color = colorOf(level);
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                SpannableString ss = new SpannableString(text);
                ss.setSpan(new ForegroundColorSpan(color), 0, ss.length(),
                        Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                if (logText.length() > MAX_LOG_CHARS) {
                    logText.setText("");
                }
                logText.append(ss);
                logScroll.post(new Runnable() {
                    @Override
                    public void run() {
                        logScroll.fullScroll(View.FOCUS_DOWN);
                    }
                });
            }
        });
    }

    private int colorOf(String level) {
        if ("ok".equals(level)) {
            return getColor(R.color.ok);
        }
        if ("err".equals(level)) {
            return getColor(R.color.err);
        }
        if ("muted".equals(level)) {
            return getColor(R.color.muted);
        }
        return getColor(R.color.text);
    }

    // ------------------------------------------------------------ 配置

    private void loadConfig() {
        SharedPreferences sp = getSharedPreferences(SP_NAME, MODE_PRIVATE);
        String ip = sp.getString("ip", "");
        if (ip != null && !ip.isEmpty()) {
            ipInput.setText(ip);
        }
        restoringConfig = true;
        rememberBox.setChecked(sp.getBoolean("remember", false));
        restoringConfig = false;
    }

    private void saveConfig(String ip) {
        SharedPreferences.Editor e = getSharedPreferences(SP_NAME, MODE_PRIVATE).edit();
        boolean remember = rememberBox.isChecked();
        e.putBoolean("remember", remember);
        e.putString("ip", remember ? ip : "");
        e.apply();
        statusText.setText(remember && ip != null && !ip.isEmpty() ? "已记住 " + ip : "");
    }

    @Override
    protected void onDestroy() {
        if (stopFlag != null) {
            stopFlag.stop();
        }
        keepAwake(false);
        super.onDestroy();
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c != null) {
            try {
                c.close();
            } catch (Exception ignored) {
            }
        }
    }
}
