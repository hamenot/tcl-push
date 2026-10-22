package com.hamenot.tclpush;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 与电视「多屏互动」服务通信的协议层。
 *
 * 报文格式：4 字节大端长度前缀 + UTF-8 正文，正文内字段用 >> 分隔。
 * 这段逻辑与 Windows 版 tcl_push.py 的 push_to_tv() 一一对应，
 * 刻意不依赖任何 Android API，方便在 PC 上用假电视做端到端回归。
 */
public final class PushCore {

    public static final int TV_PORT = 6553;
    public static final int DEFAULT_NO_PROGRESS = 60;   // 秒：电视多久没进展就放弃
    public static final int DEFAULT_HARD = 600;         // 秒：兜底总时长

    /** 推送过程中的回调，全部在调用线程（工作线程）上触发。 */
    public interface Listener {
        void log(String line, String level);   // level: "" / "ok" / "err" / "muted"

        void stage(String stage);              // confirm / download / install
    }

    /** 停止开关，UI 线程置位、工作线程轮询。 */
    public static final class StopFlag {
        private volatile boolean stop;

        public void stop() {
            stop = true;
        }

        public boolean stopped() {
            return stop;
        }
    }

    public static final class Result {
        public final String status;    // ok / denied / failed / stopped / timeout / error
        public final String message;

        Result(String status, String message) {
            this.status = status;
            this.message = message;
        }

        public boolean ok() {
            return "ok".equals(status);
        }

        @Override
        public String toString() {
            return status + " · " + message;
        }
    }

    private PushCore() {
    }

    // ---------------------------------------------------------------- 报文

    static byte[] frame(String text) {
        byte[] body = text.getBytes(StandardCharsets.UTF_8);
        int n = body.length;
        byte[] out = new byte[4 + n];
        out[0] = (byte) (n >>> 24);
        out[1] = (byte) (n >>> 16);
        out[2] = (byte) (n >>> 8);
        out[3] = (byte) n;
        System.arraycopy(body, 0, out, 4, n);
        return out;
    }

    private static void send(OutputStream out, String text) throws IOException {
        out.write(frame(text));
        out.flush();
    }

    /** 从缓冲里切出完整报文，剩余的不完整字节留在 buffer 里。 */
    static int drain(ByteArrayOutputStream buf, java.util.List<String> out) {
        byte[] b = buf.toByteArray();
        int pos = 0;
        while (b.length - pos >= 4) {
            int n = ((b[pos] & 0xFF) << 24) | ((b[pos + 1] & 0xFF) << 16)
                    | ((b[pos + 2] & 0xFF) << 8) | (b[pos + 3] & 0xFF);
            if (n < 0 || b.length - pos - 4 < n) {
                break;
            }
            out.add(new String(b, pos + 4, n, StandardCharsets.UTF_8));
            pos += 4 + n;
        }
        if (pos > 0) {
            buf.reset();
            buf.write(b, pos, b.length - pos);
        }
        return pos;
    }

    static String md5(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("MD5");
            byte[] d = md.digest(s.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(d.length * 2);
            for (byte x : d) {
                sb.append(Character.forDigit((x >> 4) & 0xF, 16));
                sb.append(Character.forDigit(x & 0xF, 16));
            }
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    /** 应用名里不能带分隔符和引号，否则电视端解析会错位。保留中文。 */
    static String safeLabel(String label, String pkg) {
        if (label == null) {
            label = "";
        }
        String s = label.replaceAll("[^\\p{L}\\p{N}_\\-. ]", "").trim();
        if (s.isEmpty()) {
            String[] parts = pkg.split("\\.");
            s = parts.length > 0 ? parts[parts.length - 1] : "app";
        }
        return s;
    }

    // ---------------------------------------------------------------- 推送

    /**
     * 执行一次推送，阻塞直到有结论。
     *
     * @param sizeBytes APK 字节数，协议里按 MB 向上取整
     */
    public static Result push(String ip, String url, String pkg, String label, long versionCode,
                              long sizeBytes, Listener listener, StopFlag stopFlag,
                              int noProgressSeconds, int hardSeconds) {
        String labelSafe = safeLabel(label, pkg);
        int sizeMb = (int) Math.ceil(sizeBytes / 1048576.0);
        if (sizeMb < 1) {
            sizeMb = 1;
        }
        long noProgress = (noProgressSeconds > 0 ? noProgressSeconds : DEFAULT_NO_PROGRESS) * 1000L;
        long hard = (hardSeconds > 0 ? hardSeconds : DEFAULT_HARD) * 1000L;

        Socket sock = new Socket();
        try {
            sock.connect(new InetSocketAddress(ip, TV_PORT), 6000);
            sock.setSoTimeout(1000);
        } catch (IOException e) {
            closeQuietly(sock);
            return new Result("error", "连不上电视的多屏互动端口：" + shortErr(e));
        }

        try {
            OutputStream os = sock.getOutputStream();
            InputStream is = sock.getInputStream();

            send(os, "159>>1>>0");
            send(os, join("223", "INSTALL", pkg, labelSafe, url, "2",
                    String.valueOf(versionCode), String.valueOf(sizeMb),
                    "", "", "", md5(url)));

            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] chunk = new byte[16384];
            java.util.List<String> msgs = new java.util.ArrayList<String>();

            long started = System.currentTimeMillis();
            long lastBeat = started;
            long lastProgress = started;
            boolean installSeen = false;

            while (true) {
                if (stopFlag != null && stopFlag.stopped()) {
                    return new Result("stopped", "已手动停止");
                }
                long now = System.currentTimeMillis();
                if (now - lastProgress > noProgress) {
                    break;
                }
                if (now - started > hard) {
                    break;
                }

                try {
                    int n = is.read(chunk);
                    if (n < 0) {
                        break;
                    }
                    if (n > 0) {
                        buf.write(chunk, 0, n);
                    }
                } catch (java.net.SocketTimeoutException ignored) {
                    // 每秒醒一次，用来检查超时与心跳
                }

                msgs.clear();
                drain(buf, msgs);
                for (String msg : msgs) {
                    String[] p = msg.split(">>", -1);
                    if (p.length < 5 || !"223".equals(p[0]) || !pkg.equals(p[2])) {
                        continue;      // 包名对不上的消息一律忽略，避免串台
                    }
                    lastProgress = System.currentTimeMillis();
                    String status = p[4];

                    if ("INSTALL".equals(p[1])) {
                        if ("confirm_pending".equals(status)) {
                            emitStage(listener, "confirm");
                        } else if ("installing".equals(status)) {
                            installSeen = true;
                            emitStage(listener, "install");
                        } else if ("installed".equals(status)) {
                            return new Result("ok", "电视报告安装完成");
                        } else if ("denied".equals(status)) {
                            return new Result("denied", "电视上选择了拒绝");
                        }
                    } else if ("PROGRESS".equals(p[1])) {
                        if ("1".equals(status)) {
                            emitStage(listener, "download");
                        } else if ("3".equals(status)) {
                            installSeen = true;
                            emitStage(listener, "install");
                        } else if ("5".equals(status)) {
                            return new Result("ok", "电视报告安装完成");
                        } else if ("2".equals(status)) {
                            return new Result("failed", "电视下载或安装失败");
                        }
                    }
                }

                if (System.currentTimeMillis() - lastBeat > 5000) {
                    try {
                        send(os, "150>>");
                    } catch (IOException e) {
                        break;
                    }
                    lastBeat = System.currentTimeMillis();
                }
            }

            if (installSeen) {
                return new Result("timeout", "电视进入安装阶段后没有再回话");
            }
            return new Result("timeout", "电视没有回传最终状态");
        } catch (IOException e) {
            return new Result("error", shortErr(e));
        } finally {
            closeQuietly(sock);
        }
    }

    private static void emitStage(Listener l, String stage) {
        if (l != null) {
            l.stage(stage);
        }
    }

    private static String join(String... parts) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.length; i++) {
            if (i > 0) {
                sb.append(">>");
            }
            sb.append(parts[i]);
        }
        return sb.toString();
    }

    private static String shortErr(Exception e) {
        String m = e.getMessage();
        return (m == null || m.isEmpty()) ? e.getClass().getSimpleName() : m;
    }

    private static void closeQuietly(Socket s) {
        try {
            s.close();
        } catch (IOException ignored) {
        }
    }

    static String now() {
        return new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
    }
}
