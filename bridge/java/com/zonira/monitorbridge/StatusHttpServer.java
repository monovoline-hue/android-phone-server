package com.zonira.monitorbridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;

/**
 * Single-purpose HTTP server bound to 127.0.0.1:8765 ONLY.
 *
 * Binding explicitly to the loopback InetAddress means the kernel itself
 * refuses connections to the LAN IP (192.168.x.x) and the Tailscale IP
 * (100.x.y.z) - there is no firewall logic to get wrong.
 *
 * One thread per connection, each capped at 2 s. At the expected load
 * (1 request per minute from collect.sh) idle CPU is effectively zero:
 * the accept() call blocks, nothing runs until a request arrives.
 */
public final class StatusHttpServer {

    public static final int PORT = 8765;
    private static final String BIND_HOST = "127.0.0.1";

    private ServerSocket server;
    private Thread acceptLoop;

    /** @return true when the server is listening. */
    public synchronized boolean start() {
        if (server != null && !server.isClosed()) {
            return true;
        }
        try {
            server = new ServerSocket(PORT, 8, InetAddress.getByName(BIND_HOST));
        } catch (Exception e) {
            return false;
        }
        acceptLoop = new Thread(this::loop, "bridge-accept");
        acceptLoop.setDaemon(true);
        acceptLoop.start();
        return true;
    }

    public synchronized void stop() {
        try {
            if (server != null) {
                server.close();
            }
        } catch (Exception ignored) {
        }
        server = null;
    }

    public boolean isAlive() {
        return server != null && !server.isClosed();
    }

    private void loop() {
        while (isAlive()) {
            try {
                Socket s = server.accept();
                Thread t = new Thread(() -> handle(s), "bridge-conn");
                t.setDaemon(true);
                t.start();
            } catch (Exception e) {
                if (isAlive()) {
                    sleep(500); // transient accept failure - back off briefly
                }
            }
        }
    }

    private void handle(Socket s) {
        try {
            s.setSoTimeout(2000);
            s.setTcpNoDelay(true);

            String path = readRequestPath(s);
            String body;
            int code;
            if ("/status".equals(path)) {
                body = DeviceStatus.json(AppContextHolder.get());
                code = 200;
            } else {
                body = "{\"error\":\"not found\",\"hint\":\"GET /status\"}";
                code = 404;
            }

            byte[] out = body.getBytes(StandardCharsets.UTF_8);
            String head = "HTTP/1.1 " + code + " " + (code == 200 ? "OK" : "Not Found") + "\r\n"
                    + "Content-Type: application/json; charset=utf-8\r\n"
                    + "Content-Length: " + out.length + "\r\n"
                    + "Connection: close\r\n\r\n";
            OutputStream os = s.getOutputStream();
            os.write(head.getBytes(StandardCharsets.UTF_8));
            os.write(out);
            os.flush();
        } catch (SocketTimeoutException e) {
            // client hung up or went silent - nothing to do
        } catch (Exception ignored) {
        } finally {
            try {
                s.close();
            } catch (Exception ignored) {
            }
        }
    }

    /** Reads only what is needed: the request line. Header/body discarded. */
    private static String readRequestPath(Socket s) throws Exception {
        BufferedReader r = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.US_ASCII));
        String line = r.readLine();
        if (line == null) {
            return "";
        }
        // "GET /status HTTP/1.1" -> "/status"
        String[] parts = line.split(" ");
        return parts.length >= 2 ? parts[1] : "";
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
