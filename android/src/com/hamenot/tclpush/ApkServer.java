package com.hamenot.tclpush;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 给电视提供下载的最简 HTTP 服务。
 *
 * 电视会主动来 GET 一次 APK，所以手机上必须有人监听端口。这里只实现电视真正
 * 会用到的那部分：GET / HEAD、Content-Length、Range（有电视会试探续传）。
 * 不用第三方库，几十行足够，也免得为它引一堆依赖。
 */
public final class ApkServer {

    public interface Log {
        void line(String text, String level);
    }

    private final File apk;
    private final Log log;
    private final AtomicBoolean served = new AtomicBoolean(false);
    private final AtomicBoolean running = new AtomicBoolean(false);
    private ServerSocket server;
    private Thread acceptor;

    public ApkServer(File apk, Log log) {
        this.apk = apk;
        this.log = log;
    }

    /** 绑定 0.0.0.0 上的随机端口，返回实际端口。 */
    public int start() throws IOException {
        server = new ServerSocket();
        server.setReuseAddress(true);
        server.bind(new InetSocketAddress("0.0.0.0", 0), 20);
        running.set(true);
        acceptor = new Thread(new Runnable() {
            @Override
            public void run() {
                acceptLoop();
            }
        }, "apk-http");
        acceptor.setDaemon(true);
        acceptor.start();
        return server.getLocalPort();
    }

    public boolean served() {
        return served.get();
    }

    public void stop() {
        running.set(false);
        try {
            if (server != null) {
                server.close();
            }
        } catch (IOException ignored) {
        }
    }

    private void acceptLoop() {
        while (running.get()) {
            Socket s = null;
            try {
                s = server.accept();
                handle(s);
            } catch (IOException e) {
                if (running.get()) {
                    say("HTTP 服务异常：" + e.getMessage(), "err");
                }
            } finally {
                if (s != null) {
                    try {
                        s.close();
                    } catch (IOException ignored) {
                    }
                }
            }
        }
    }

    private void handle(Socket s) throws IOException {
        s.setSoTimeout(15000);
        InputStream in = new BufferedInputStream(s.getInputStream());
        OutputStream out = new BufferedOutputStream(s.getOutputStream(), 32768);

        String requestLine = readLine(in);
        if (requestLine == null) {
            return;
        }
        String rangeHeader = null;
        long contentLength = 0;
        // 读完请求头，顺便取出 Range
        while (true) {
            String h = readLine(in);
            if (h == null || h.isEmpty()) {
                break;
            }
            int c = h.indexOf(':');
            if (c > 0) {
                String k = h.substring(0, c).trim().toLowerCase();
                String v = h.substring(c + 1).trim();
                if ("range".equals(k)) {
                    rangeHeader = v;
                } else if ("content-length".equals(k)) {
                    try {
                        contentLength = Long.parseLong(v);
                    } catch (NumberFormatException ignored) {
                    }
                }
            }
        }

        String remote = s.getInetAddress() == null ? "?" : s.getInetAddress().getHostAddress();
        served.set(true);

        String[] parts = requestLine.split(" ");
        String method = parts.length > 0 ? parts[0] : "GET";
        say("电视 " + remote + " 来取文件（" + method + "）", "muted");

        long total = apk.length();
        long start = 0;
        long end = total - 1;
        boolean partial = false;

        if (rangeHeader != null && rangeHeader.startsWith("bytes=")) {
            String spec = rangeHeader.substring(6).trim();
            int dash = spec.indexOf('-');
            if (dash >= 0) {
                try {
                    String a = spec.substring(0, dash).trim();
                    String b = spec.substring(dash + 1).trim();
                    if (!a.isEmpty()) {
                        start = Long.parseLong(a);
                    }
                    if (!b.isEmpty()) {
                        end = Long.parseLong(b);
                    }
                    if (end >= total) {
                        end = total - 1;
                    }
                    if (start <= end) {
                        partial = true;
                    }
                } catch (NumberFormatException ignored) {
                    start = 0;
                    end = total - 1;
                    partial = false;
                }
            }
        }

        if (start > end || start >= total) {
            writeAscii(out, "HTTP/1.1 416 Range Not Satisfiable\r\n" +
                    "Content-Range: bytes */" + total + "\r\n" +
                    "Connection: close\r\n\r\n");
            out.flush();
            return;
        }

        long len = end - start + 1;
        StringBuilder head = new StringBuilder();
        head.append(partial ? "HTTP/1.1 206 Partial Content\r\n" : "HTTP/1.1 200 OK\r\n");
        head.append("Content-Type: application/vnd.android.package-archive\r\n");
        head.append("Content-Length: ").append(len).append("\r\n");
        head.append("Accept-Ranges: bytes\r\n");
        if (partial) {
            head.append("Content-Range: bytes ").append(start).append('-').append(end)
                    .append('/').append(total).append("\r\n");
        }
        head.append("Connection: close\r\n\r\n");
        writeAscii(out, head.toString());

        if (!"HEAD".equalsIgnoreCase(method)) {
            FileInputStream fis = new FileInputStream(apk);
            try {
                long skipped = 0;
                while (skipped < start) {
                    long n = fis.skip(start - skipped);
                    if (n <= 0) {
                        break;
                    }
                    skipped += n;
                }
                byte[] buf = new byte[32768];
                long left = len;
                while (left > 0) {
                    int n = fis.read(buf, 0, (int) Math.min(buf.length, left));
                    if (n < 0) {
                        break;
                    }
                    out.write(buf, 0, n);
                    left -= n;
                }
            } finally {
                fis.close();
            }
        }
        out.flush();
    }

    private static String readLine(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int c;
        while ((c = in.read()) >= 0) {
            if (c == '\n') {
                int len = sb.length();
                if (len > 0 && sb.charAt(len - 1) == '\r') {
                    sb.setLength(len - 1);
                }
                return sb.toString();
            }
            if (sb.length() > 8192) {
                break;
            }
            sb.append((char) c);
        }
        return sb.length() == 0 ? null : sb.toString();
    }

    private static void writeAscii(OutputStream out, String s) throws IOException {
        out.write(s.getBytes(StandardCharsets.ISO_8859_1));
    }

    private void say(String text, String level) {
        if (log != null) {
            log.line(text, level);
        }
    }
}
