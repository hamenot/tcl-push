import com.hamenot.tclpush.ApkServer;
import com.hamenot.tclpush.NetUtil;
import com.hamenot.tclpush.PushCore;

import java.io.File;

/**
 * PC 端回归用的命令行客户端（不进 APK）。
 *
 * 用法：java PushCli <ip> <apkPath> <pkg> <label> <versionCode> [noProgressSec] [hardSec] [stopAfterSec]
 *
 * 它做的事和 Android 界面里的「推送」按钮完全一样：起 HTTP 服务、拼 URL、
 * 走 PushCore 的协议。事件按行打到 stdout，方便测试脚本断言。
 */
public class PushCli {

    public static void main(String[] args) throws Exception {
        if (args.length < 5) {
            System.out.println("usage: PushCli <ip> <apkPath> <pkg> <label> <versionCode> [noProgress] [hard]");
            System.exit(2);
        }
        String ip = args[0];
        File apk = new File(args[1]);
        String pkg = args[2];
        String label = args[3];
        long vc = Long.parseLong(args[4]);
        int noProgress = args.length > 5 ? Integer.parseInt(args[5]) : PushCore.DEFAULT_NO_PROGRESS;
        int hard = args.length > 6 ? Integer.parseInt(args[6]) : PushCore.DEFAULT_HARD;
        int stopAfter = args.length > 7 ? Integer.parseInt(args[7]) : 0;

        final Object lock = new Object();
        ApkServer.Log httpLog = new ApkServer.Log() {
            @Override
            public void line(String text, String level) {
                synchronized (lock) {
                    System.out.println("[http] " + text);
                    System.out.flush();
                }
            }
        };

        ApkServer srv = new ApkServer(apk, httpLog);
        int port = srv.start();
        String ipLocal = System.getenv("TCLPUSH_LOCAL_IP");
        if (ipLocal == null || ipLocal.trim().isEmpty()) {
            ipLocal = NetUtil.lanIp();
        }
        String url = "http://" + ipLocal + ":" + port + "/app.apk";
        System.out.println("[url] " + url);
        System.out.println("[size] " + apk.length());
        System.out.flush();

        PushCore.Listener listener = new PushCore.Listener() {
            @Override
            public void log(String line, String level) {
                synchronized (lock) {
                    System.out.println("[log] " + line + " | " + level);
                    System.out.flush();
                }
            }

            @Override
            public void stage(String stage) {
                synchronized (lock) {
                    System.out.println("[stage] " + stage);
                    System.out.flush();
                }
            }
        };

        final PushCore.StopFlag stopFlag = new PushCore.StopFlag();
        if (stopAfter > 0) {
            final int delay = stopAfter;
            Thread stopper = new Thread(new Runnable() {
                @Override
                public void run() {
                    try {
                        Thread.sleep(delay * 1000L);
                    } catch (InterruptedException ignored) {
                    }
                    System.out.println("[stop] 手动停止");
                    System.out.flush();
                    stopFlag.stop();
                }
            });
            stopper.setDaemon(true);
            stopper.start();
        }

        long t0 = System.currentTimeMillis();
        PushCore.Result r = PushCore.push(ip, url, pkg, label, vc, apk.length(),
                listener, stopFlag, noProgress, hard);
        long ms = System.currentTimeMillis() - t0;

        System.out.println("[served] " + srv.served());
        System.out.println("[elapsed] " + ms);
        System.out.println("[result] " + r.status + " | " + r.message);
        System.out.flush();
        srv.stop();
        System.exit(r.ok() ? 0 : 1);
    }
}
