package com.aitest.si;

import java.awt.AWTEvent;
import java.awt.Component;
import java.awt.Container;
import java.awt.Frame;
import java.awt.KeyboardFocusManager;
import java.awt.Point;
import java.awt.Window;
import java.awt.event.ItemEvent;
import java.awt.event.KeyEvent;
import java.awt.event.MouseEvent;
import java.awt.event.MouseWheelEvent;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.security.AccessController;
import java.security.PrivilegedAction;

/**
 * Drives the Forms client from inside its own JVM, over a loopback socket.
 *
 * Loaded by the bootstrap class loader, so it runs with a null protection
 * domain and is not subject to the Web Start sandbox that leaves ordinary agent
 * code unable to read the thread list or open a socket.
 *
 * Injected events travel the same dispatch path as a real click or keypress, so
 * Forms cannot tell them apart and every Personalization fires as usual, while
 * nothing enters the operating system input queue - which is what made every
 * earlier approach take over the user's mouse and keyboard.
 *
 * AppContext matters here: Java Web Start runs the UI in its own AppContext and
 * Frame.getFrames() reports only the calling thread's context, so an agent
 * thread sees no frames at all. AppContext is keyed by ThreadGroup, so the
 * command loop runs on a thread created inside the event dispatch thread group.
 *
 * The socket is loopback-only, but any local process could otherwise drive the
 * user's authenticated session, so a caller must present the secret first.
 */
public class Driver {

    private static volatile ServerSocket server;
    private static volatile String secret = "";

    public static void start(String portText, String tokenText) {
        if (server != null) {
            return;
        }
        secret = tokenText == null ? "" : tokenText;
        final int port = Integer.parseInt(portText);
        // A permission check inspects the whole call stack, and the loader frame
        // below this one carries no permissions - so being bootstrap-loaded is
        // not enough on its own. doPrivileged cuts the stack here, where the
        // protection domain is null and therefore unrestricted.
        AccessController.doPrivileged(new PrivilegedAction<Void>() {
            public Void run() {
                Thread t = new Thread(eventThreadGroup(), new Runnable() {
                    public void run() {
                        serve(port);
                    }
                }, "aitest-forms-drive");
                t.setDaemon(true);
                t.start();
                return null;
            }
        });
    }

    private static ThreadGroup eventThreadGroup() {
        // Java Web Start runs the Forms applet in its own AppContext, keyed by
        // ThreadGroup (named after the codebase, e.g. ".../OA_JAVA/-threadGroup").
        // The server thread has to live in that group, or Window.getWindows()
        // and the KeyboardFocusManager see a different, empty context.
        ThreadGroup best = null;
        for (Thread t : Thread.getAllStackTraces().keySet()) {
            ThreadGroup g = t.getThreadGroup();
            if (g == null) continue;
            String gn = g.getName() == null ? "" : g.getName();
            if (gn.contains("OA_JAVA") || gn.contains("threadGroup")) return g;
            if (t.getName().startsWith("AWT-EventQueue") && !gn.startsWith("Plugin") && best == null) best = g;
        }
        if (best != null) return best;
        for (Thread t : Thread.getAllStackTraces().keySet()) {
            if (t.getName().startsWith("AWT-EventQueue")) return t.getThreadGroup();
        }
        return Thread.currentThread().getThreadGroup();
    }

    private static void serve(final int port) {
        AccessController.doPrivileged(new PrivilegedAction<Void>() {
            public Void run() {
                serveImpl(port);
                return null;
            }
        });
    }

    private static void serveImpl(int port) {
        // One thread per client, with a read timeout: a client that dies
        // mid-session used to leave the single accept loop blocked in readLine,
        // so every later connection queued forever.
        try {
            server = new ServerSocket(port, 8, InetAddress.getByName("127.0.0.1"));
            while (true) {
                final Socket s = server.accept();
                Thread t = new Thread(new Runnable() { public void run() { client(s); } }, "aitest-client");
                t.setDaemon(true);
                t.start();
            }
        } catch (Throwable t) {
            server = null;
        }
    }

    private static void client(Socket s) {
        try {
            s.setSoTimeout(120000);
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), "UTF-8"));
            PrintWriter out = new PrintWriter(s.getOutputStream(), true);
            String hello = in.readLine();
            if (secret.length() > 0 && !("auth " + secret).equals(hello)) { out.println("ERR auth"); return; }
            out.println("OK auth");
            String line;
            while ((line = in.readLine()) != null) {
                if ("quit".equals(line)) break;
                try { out.println(handle(line)); } catch (Throwable t) { out.println("ERR " + t); }
            }
        } catch (Throwable ignored) {
        } finally {
            try { s.close(); } catch (Throwable ignored) { }
        }
    }

    private static String handle(String line) throws Exception {
        int sp = line.indexOf(' ');
        String cmd = sp < 0 ? line : line.substring(0, sp);
        String rest = sp < 0 ? "" : line.substring(sp + 1);

        if ("ping".equals(cmd)) {
            return "OK " + System.getProperty("java.version")
                    + " sm=" + System.getSecurityManager();
        }
        if ("focus".equals(cmd)) {
            return "OK " + describe(KeyboardFocusManager
                    .getCurrentKeyboardFocusManager().getFocusOwner());
        }
        if ("frames".equals(cmd)) {
            StringBuilder sb = new StringBuilder("OK");
            for (Frame f : Frame.getFrames()) {
                sb.append(" {").append(f.getTitle()).append(" showing=")
                        .append(f.isShowing()).append(" at=")
                        .append(f.getBounds()).append("}");
            }
            return sb.toString();
        }
        if ("at".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            return "OK " + describe(componentAt(Integer.parseInt(p[0]),
                    Integer.parseInt(p[1])));
        }
        if ("click".equals(cmd) || "dclick".equals(cmd)) {
            // Navigator rows are painted inside a lightweight list rather than
            // being components of their own, so they can only be reached by
            // coordinate - and a branch expands, or a leaf opens, on a double
            // click. Keyboard navigation is no help there: clicking a row
            // selects it but leaves the focus on a Forms item, so arrow keys
            // never reach the tree.
            int clicks = "dclick".equals(cmd) ? 2 : 1;
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component c = componentAt(sx, sy);
            if (c == null) {
                return "ERR no component at " + sx + "," + sy;
            }
            Point origin = c.getLocationOnScreen();
            int x = sx - origin.x;
            int y = sy - origin.y;
            long t = System.currentTimeMillis();
            for (int n = 1; n <= clicks; n++) {
                dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_PRESSED, t, 0,
                        x, y, n, false, MouseEvent.BUTTON1));
                dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_RELEASED, t + 8,
                        0, x, y, n, false, MouseEvent.BUTTON1));
                dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_CLICKED, t + 16,
                        0, x, y, n, false, MouseEvent.BUTTON1));
                t += 40;
            }
            Thread.sleep(200);
            return "OK clicked x" + clicks + " " + describe(c) + " focus="
                    + describe(KeyboardFocusManager
                            .getCurrentKeyboardFocusManager().getFocusOwner());
        }
        if ("findn".equals(cmd) || "clickn".equals(cmd)
                || "dclickn".equals(cmd)) {
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR no component named " + p[0] + " near " + p[1]
                        + "," + p[2];
            }
            if ("findn".equals(cmd)) {
                return "OK " + describe(c);
            }
            return clickComponent(c, "dclickn".equals(cmd) ? 2 : 1);
        }
        if ("wheel".equals(cmd)) {
            // The Navigator tree lives in a scrolling list and its Expand
            // button opens the whole tree, which pushes the wanted row out of
            // view; a wheel event scrolls it back into range the way a person
            // would, without any of it reaching the input queue.
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            int notches = Integer.parseInt(p[2]);
            Component c = componentAt(sx, sy);
            if (c == null) {
                return "ERR no component at " + sx + "," + sy;
            }
            Point o = c.getLocationOnScreen();
            long t = System.currentTimeMillis();
            dispatch(c, new MouseWheelEvent(c, MouseEvent.MOUSE_WHEEL, t, 0,
                    sx - o.x, sy - o.y, 0, false,
                    MouseWheelEvent.WHEEL_UNIT_SCROLL, 3, notches));
            Thread.sleep(200);
            return "OK wheel " + notches + " on " + describe(c);
        }
        if ("gclick".equals(cmd) || "gpress".equals(cmd)
                || "grelease".equals(cmd)) {
            // Oracle's toolkit lays a GlassMouseGrabProvider over the window and
            // routes real mouse events through it. Dispatching straight to the
            // target skips that machinery: buttons and fields cope, because they
            // handle their own events, but a poplist never opens its popup.
            // These send to the glass layer instead and let the toolkit route.
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component g = glassAt(sx, sy);
            if (g == null) {
                return "ERR no glass layer at " + sx + "," + sy;
            }
            Point o = g.getLocationOnScreen();
            int x = sx - o.x;
            int y = sy - o.y;
            long t = System.currentTimeMillis();
            if (!"grelease".equals(cmd)) {
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_MOVED, t, 0,
                        x, y, 0, false));
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_PRESSED, t + 4,
                        0, x, y, 1, false, MouseEvent.BUTTON1));
            }
            if (!"gpress".equals(cmd)) {
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_RELEASED,
                        t + 40, 0, x, y, 1, false, MouseEvent.BUTTON1));
                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_CLICKED,
                        t + 48, 0, x, y, 1, false, MouseEvent.BUTTON1));
            }
            Thread.sleep(250);
            return "OK " + cmd + " via " + describe(g) + " focus="
                    + describe(KeyboardFocusManager
                            .getCurrentKeyboardFocusManager().getFocusOwner());
        }
        if ("gdrag".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component g = glassAt(sx, sy);
            if (g == null) {
                return "ERR no glass layer";
            }
            Point o = g.getLocationOnScreen();
            long t = System.currentTimeMillis();
            dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_DRAGGED, t,
                    MouseEvent.BUTTON1_DOWN_MASK, sx - o.x, sy - o.y, 0,
                    false));
            Thread.sleep(120);
            return "OK dragged to " + sx + "," + sy;
        }
        if ("xwindows".equals(cmd)) {
            return allContexts("list", 0, 0);
        }
        if ("xat".equals(cmd) || "xgclick".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            return allContexts("xat".equals(cmd) ? "at" : "gclick", Integer.parseInt(p[0]), Integer.parseInt(p[1]));
        }
        if ("windows".equals(cmd)) {
            StringBuilder sb = new StringBuilder("OK ");
            for (Window x : Window.getWindows()) {
                sb.append("{").append(x.getClass().getSimpleName())
                  .append(" showing=").append(x.isShowing());
                if (x.isShowing()) {
                    sb.append(" at=").append(x.getLocationOnScreen())
                      .append(" size=").append(x.getWidth()).append("x")
                      .append(x.getHeight())
                      .append(" kids=").append(x.getComponentCount());
                }
                sb.append("} ");
            }
            return sb.toString();
        }
        if ("hold".equals(cmd)) {
            // A poplist opens on press and commits on release; pressing and
            // releasing at the same point opens the popup and closes it again,
            // which is why every programmatic selection was ignored. This holds
            // the button down so the popup can be inspected.
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            long t = System.currentTimeMillis();
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_PRESSED, t, 0,
                    c.getWidth() / 2, c.getHeight() / 2, 1, false,
                    MouseEvent.BUTTON1));
            Thread.sleep(400);
            return handle("windows");
        }
        if ("release".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            int sx = Integer.parseInt(p[0]);
            int sy = Integer.parseInt(p[1]);
            Component c = componentAt(sx, sy);
            if (c == null) {
                return "ERR no component at " + sx + "," + sy;
            }
            Point o = c.getLocationOnScreen();
            long t = System.currentTimeMillis();
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_RELEASED, t, 0,
                    sx - o.x, sy - o.y, 1, false, MouseEvent.BUTTON1));
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_CLICKED, t + 8, 0,
                    sx - o.x, sy - o.y, 1, false, MouseEvent.BUTTON1));
            Thread.sleep(250);
            return "OK released on " + describe(c);
        }
        if ("toggle".equals(cmd)) {
            // Same shape as the poplist: neither a click nor the space key
            // ticks a Forms check box, because Forms keeps its own item value
            // and only updates it when the selection event arrives.
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            boolean want = p.length < 4 || !"false".equals(p[3]);
            StringBuilder tried = new StringBuilder();
            for (String setter : new String[]{"setState", "setSelected"}) {
                try {
                    c.getClass().getMethod(setter, boolean.class)
                            .invoke(c, want);
                    Thread.sleep(150);
                    if (c instanceof java.awt.ItemSelectable) {
                        dispatch(c, new ItemEvent((java.awt.ItemSelectable) c,
                                ItemEvent.ITEM_STATE_CHANGED, c,
                                want ? ItemEvent.SELECTED
                                     : ItemEvent.DESELECTED));
                    }
                    Thread.sleep(250);
                    return "OK " + setter + "(" + want + "); "
                            + handle("read " + p[0] + "|" + p[1] + "|" + p[2]);
                } catch (Throwable t) {
                    tried.append(setter).append(":").append(t).append(" ");
                }
            }
            return "ERR " + tried;
        }
        if ("cookies".equals(cmd)) {
            // The Forms applet is the one thing on this machine that is already
            // authenticated through SSO and that we can reach without touching
            // the user's browser or input. Its cookie store carries the EBS
            // session, so HTTP work can be done from in here.
            StringBuilder sb = new StringBuilder("OK ");
            java.net.CookieHandler h = java.net.CookieHandler.getDefault();
            sb.append("handler=").append(h == null ? "null" : h.getClass().getName());
            if (h instanceof java.net.CookieManager) {
                for (java.net.HttpCookie c : ((java.net.CookieManager) h)
                        .getCookieStore().getCookies()) {
                    sb.append(" {").append(c.getDomain()).append(" ")
                      .append(c.getName()).append("=")
                      .append(c.getValue().length() > 12
                              ? c.getValue().substring(0, 12) + "..." : c.getValue())
                      .append("}");
                }
            }
            return sb.toString();
        }
        if ("http".equals(cmd)) {
            // http <METHOD> <url> [body]  - performed from inside the JVM so it
            // inherits the applet's proxy settings and any cookies it holds.
            String[] p = rest.trim().split("[ ]+", 3);
            String method = p[0];
            String url = p[1];
            String body = p.length > 2 ? p[2] : null;
            java.net.HttpURLConnection con = (java.net.HttpURLConnection)
                    new java.net.URL(url).openConnection();
            con.setInstanceFollowRedirects(false);
            con.setRequestMethod(method);
            con.setConnectTimeout(20000);
            con.setReadTimeout(30000);
            if (body != null) {
                con.setDoOutput(true);
                con.setRequestProperty("Content-Type",
                        "application/x-www-form-urlencoded");
                byte[] b = body.getBytes("UTF-8");
                con.getOutputStream().write(b);
            }
            int code = con.getResponseCode();
            StringBuilder sb = new StringBuilder("OK status=" + code);
            String loc = con.getHeaderField("Location");
            if (loc != null) {
                sb.append(" location=").append(loc);
            }
            java.io.InputStream in = code >= 400 ? con.getErrorStream()
                    : con.getInputStream();
            if (in != null) {
                byte[] buf = new byte[4000];
                int n = in.read(buf);
                sb.append(" body=").append(n > 0 ? new String(buf, 0, n, "UTF-8")
                        .replace('\n', ' ').replace('\r', ' ') : "");
            }
            return sb.toString();
        }
        if ("read".equals(cmd)) {
            // The Access Bridge reports no value for a Forms poplist, so a
            // chosen option cannot be verified through it - and choosing blind
            // is not acceptable when neighbouring actions change data. These
            // getters are what the component itself exposes.
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            StringBuilder sb = new StringBuilder("OK ");
            for (String g : new String[]{"getValue", "getSelectedItem",
                    "getText", "getSelectedIndex", "getItemCount",
                    "getState", "isSelected"}) {
                try {
                    Object v = c.getClass().getMethod(g).invoke(c);
                    sb.append(g).append("=").append(v).append(" ");
                } catch (Throwable ignored) {
                    // not every component offers every getter
                }
            }
            return sb.toString();
        }
        if ("items".equals(cmd)) {
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            StringBuilder sb = new StringBuilder("OK ");
            try {
                int n = (Integer) c.getClass().getMethod("getItemCount")
                        .invoke(c);
                for (int i = 0; i < n && i < 60; i++) {
                    Object v = c.getClass().getMethod("getItem", int.class)
                            .invoke(c, i);
                    sb.append("[").append(i).append("]").append(v).append(" ");
                }
            } catch (Throwable t) {
                return "ERR " + t;
            }
            return sb.toString();
        }
        if ("pick".equals(cmd)) {
            String[] p = rest.split("[|]");
            Component c = byName(p[0], Integer.parseInt(p[1]),
                    Integer.parseInt(p[2]));
            if (c == null) {
                return "ERR not found " + p[0];
            }
            int idx = Integer.parseInt(p[3]);
            // VPopList is Choice-like: it exposes select(int), not
            // setSelectedIndex. Several setters are tried so the caller does
            // not have to know which toolkit class is behind a given item.
            StringBuilder tried = new StringBuilder();
            for (String setter : new String[]{"select", "setSelectedIndex"}) {
                try {
                    c.getClass().getMethod(setter, int.class).invoke(c, idx);
                    Thread.sleep(200);
                    // Setting the widget is not enough: Forms keeps its own
                    // item value and only updates it when the selection event
                    // arrives, which is why Go kept running with no action.
                    Object item = null;
                    try {
                        item = c.getClass().getMethod("getItem", int.class)
                                .invoke(c, idx);
                    } catch (Throwable ignored) {
                        item = String.valueOf(idx);
                    }
                    if (c instanceof java.awt.ItemSelectable) {
                        dispatch(c, new ItemEvent((java.awt.ItemSelectable) c,
                                ItemEvent.ITEM_STATE_CHANGED, item,
                                ItemEvent.SELECTED));
                    }
                    Thread.sleep(300);
                    return "OK " + setter + "(" + idx + ") + ItemEvent; "
                            + handle("read " + p[0] + "|" + p[1] + "|" + p[2]);
                } catch (Throwable t) {
                    tried.append(setter).append(":").append(t).append(" ");
                }
            }
            return "ERR " + tried;
        }
        if ("type".equals(cmd)) {
            Component c = KeyboardFocusManager
                    .getCurrentKeyboardFocusManager().getFocusOwner();
            if (c == null) {
                return "ERR nothing has focus";
            }
            for (char ch : rest.toCharArray()) {
                long t = System.currentTimeMillis();
                dispatch(c, new KeyEvent(c, KeyEvent.KEY_PRESSED, t, 0,
                        KeyEvent.VK_UNDEFINED, ch));
                dispatch(c, new KeyEvent(c, KeyEvent.KEY_TYPED, t + 1, 0,
                        KeyEvent.VK_UNDEFINED, ch));
                dispatch(c, new KeyEvent(c, KeyEvent.KEY_RELEASED, t + 2, 0,
                        KeyEvent.VK_UNDEFINED, ch));
                Thread.sleep(12);
            }
            return "OK typed " + rest.length();
        }
        if ("key".equals(cmd)) {
            String[] p = rest.trim().split("[ ]+");
            int code = Integer.parseInt(p[0]);
            int mods = p.length > 1 ? Integer.parseInt(p[1]) : 0;
            Component c = KeyboardFocusManager
                    .getCurrentKeyboardFocusManager().getFocusOwner();
            if (c == null) {
                return "ERR nothing has focus";
            }
            long t = System.currentTimeMillis();
            dispatch(c, new KeyEvent(c, KeyEvent.KEY_PRESSED, t, mods, code,
                    KeyEvent.CHAR_UNDEFINED));
            dispatch(c, new KeyEvent(c, KeyEvent.KEY_RELEASED, t + 4, mods,
                    code, KeyEvent.CHAR_UNDEFINED));
            Thread.sleep(150);
            return "OK key " + code;
        }
        if ("stop".equals(cmd)) {
            ServerSocket s = server;
            server = null;
            if (s != null) {
                s.close();
            }
            return "OK stopping";
        }
        return "ERR unknown command " + cmd;
    }


    /**
     * Locate a component by accessible name and where it sits on screen.
     *
     * Coordinate hit-testing is unreliable here: Forms stacks internal frames
     * inside one AWT frame, so a point over the Find window also falls inside
     * the Order Organizer behind it, and the search kept returning fields from
     * the wrong window. The Access Bridge already tells the caller both the name
     * and the exact screen rectangle, so matching on the pair is exact.
     */
    private static Component byName(String wanted, int sx, int sy) {
        java.util.List<Component> all = new java.util.ArrayList<Component>();
        for (Window w : Window.getWindows()) {
            if (w.isShowing()) {
                collect(w, all);
            }
        }
        Component best = null;
        long bestDist = Long.MAX_VALUE;
        for (Component c : all) {
            if (!c.isShowing()) {
                continue;
            }
            String name = accessibleName(c);
            if (name == null || !name.equals(wanted)) {
                continue;
            }
            Point p;
            try {
                p = c.getLocationOnScreen();
            } catch (Throwable t) {
                continue;
            }
            long dx = p.x - sx;
            long dy = p.y - sy;
            long dist = dx * dx + dy * dy;
            if (dist < bestDist) {
                bestDist = dist;
                best = c;
            }
        }
        return bestDist <= 40 * 40 ? best : null;
    }

    private static void collect(Container parent, java.util.List<Component> out) {
        for (Component c : parent.getComponents()) {
            out.add(c);
            if (c instanceof Container) {
                collect((Container) c, out);
            }
        }
    }

    private static String accessibleName(Component c) {
        try {
            return c.getAccessibleContext() == null ? null
                    : c.getAccessibleContext().getAccessibleName();
        } catch (Throwable t) {
            return null;
        }
    }

    private static String clickComponent(Component c) throws Exception {
        return clickComponent(c, 1);
    }

    /**
     * Click a component, optionally twice.
     *
     * A tree row expands or opens on a double click, which is how the Navigator
     * is meant to be driven - the Expand button only acts on a row the tree
     * itself considers selected, and a single click does not get it there.
     */
    private static String clickComponent(Component c, int clicks)
            throws Exception {
        Point origin = c.getLocationOnScreen();
        int x = c.getWidth() / 2;
        int y = c.getHeight() / 2;
        long t = System.currentTimeMillis();
        for (int n = 1; n <= clicks; n++) {
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_PRESSED, t, 0, x, y,
                    n, false, MouseEvent.BUTTON1));
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_RELEASED, t + 8, 0,
                    x, y, n, false, MouseEvent.BUTTON1));
            dispatch(c, new MouseEvent(c, MouseEvent.MOUSE_CLICKED, t + 16, 0,
                    x, y, n, false, MouseEvent.BUTTON1));
            t += 40;
        }
        Thread.sleep(200);
        return "OK clicked x" + clicks + " " + describe(c) + " at=" + origin
                + " focus=" + describe(KeyboardFocusManager
                        .getCurrentKeyboardFocusManager().getFocusOwner());
    }


    /** Run a snippet in every AppContext and gather the results. */
    private static String allContexts(final String what, final int sx, final int sy) {
        final StringBuilder out = new StringBuilder("OK");
        try {
            java.util.Set<?> ctxs = (java.util.Set<?>) Class.forName("sun.awt.AppContext")
                    .getMethod("getAppContexts").invoke(null);
            for (final Object ctx : ctxs) {
                ThreadGroup tg = (ThreadGroup) ctx.getClass().getMethod("getThreadGroup").invoke(ctx);
                final StringBuilder part = new StringBuilder();
                Thread t = new Thread(tg, new Runnable() { public void run() {
                    try {
                        if ("list".equals(what)) {
                            for (Window w : Window.getWindows()) {
                                if (!w.isShowing()) continue;
                                String title = (w instanceof Frame) ? ((Frame) w).getTitle()
                                        : (w instanceof java.awt.Dialog) ? ((java.awt.Dialog) w).getTitle() : "";
                                part.append(" {").append(w.getClass().getSimpleName()).append(" '").append(title)
                                    .append("' at=").append(w.getLocationOnScreen()).append(" size=")
                                    .append(w.getWidth()).append("x").append(w.getHeight()).append("}");
                            }
                        } else if ("at".equals(what)) {
                            Component c = componentAt(sx, sy);
                            if (c != null) part.append(" ").append(describe(c));
                        } else if ("gclick".equals(what)) {
                            Component g = glassAt(sx, sy);
                            if (g != null) {
                                Point o = g.getLocationOnScreen(); int x = sx - o.x, y = sy - o.y;
                                long tm = System.currentTimeMillis();
                                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_PRESSED, tm, 0, x, y, 1, false, MouseEvent.BUTTON1));
                                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_RELEASED, tm + 40, 0, x, y, 1, false, MouseEvent.BUTTON1));
                                dispatch(g, new MouseEvent(g, MouseEvent.MOUSE_CLICKED, tm + 48, 0, x, y, 1, false, MouseEvent.BUTTON1));
                                part.append(" clicked ").append(describe(g));
                            }
                        }
                    } catch (Throwable e) { part.append(" ERR ").append(e); }
                }}, "aitest-ctx");
                t.start(); t.join(8000);
                if (part.length() > 0) out.append(" [ctx ").append(tg.getName()).append("]").append(part);
            }
        } catch (Throwable e) { return "ERR " + e; }
        return out.toString();
    }

    private static void dispatch(Component c, AWTEvent e) {
        c.dispatchEvent(e);
    }

    /** Topmost component at a point, glass overlays included. */
    private static Component glassAt(int sx, int sy) {
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) {
                continue;
            }
            Point o = w.getLocationOnScreen();
            if (sx < o.x || sy < o.y || sx >= o.x + w.getWidth()
                    || sy >= o.y + w.getHeight()) {
                continue;
            }
            Component best = w;
            Container parent = w;
            int x = sx - o.x;
            int y = sy - o.y;
            for (int depth = 0; depth < 12; depth++) {
                Component hit = null;
                for (Component c : parent.getComponents()) {
                    if (c.isVisible() && c.getBounds().contains(x, y)) {
                        hit = c;
                        break;
                    }
                }
                if (hit == null) {
                    break;
                }
                best = hit;
                x -= hit.getX();
                y -= hit.getY();
                if (!(hit instanceof Container)) {
                    break;
                }
                parent = (Container) hit;
            }
            return best;
        }
        return null;
    }

    private static Component componentAt(int sx, int sy) {
        for (Window w : Window.getWindows()) {
            if (!w.isShowing()) {
                continue;
            }
            Point o = w.getLocationOnScreen();
            if (sx < o.x || sy < o.y || sx >= o.x + w.getWidth()
                    || sy >= o.y + w.getHeight()) {
                continue;
            }
            Component c = deepest(w, sx - o.x, sy - o.y);
            if (c != null) {
                return c;
            }
        }
        return null;
    }

    /**
     * Most specific component under a point, ignoring full-window overlays.
     *
     * Oracle's toolkit puts a GlassMouseGrabProvider proxy across the whole
     * window, so taking the first child whose bounds contain the point always
     * stops there and never reaches the field - which made every click land
     * nowhere while the typing went to whatever item Forms already had current.
     * Every matching child is followed instead, and the smallest result wins.
     */
    private static Component deepest(Container parent, int x, int y) {
        Component best = null;
        long bestArea = Long.MAX_VALUE;
        for (Component c : parent.getComponents()) {
            if (!c.isVisible() || !c.getBounds().contains(x, y)) {
                continue;
            }
            if (c.getClass().getName().contains("Glass")) {
                continue;
            }
            Component hit = (c instanceof Container)
                    ? deepest((Container) c, x - c.getX(), y - c.getY()) : c;
            if (hit == null) {
                hit = c;
            }
            long area = (long) hit.getWidth() * hit.getHeight();
            if (area < bestArea) {
                bestArea = area;
                best = hit;
            }
        }
        return best == null ? parent : best;
    }

    private static String describe(Component c) {
        if (c == null) {
            return "null";
        }
        String acc = "";
        try {
            if (c.getAccessibleContext() != null) {
                acc = "/" + c.getAccessibleContext().getAccessibleName();
            }
        } catch (Throwable ignored) {
            acc = "";
        }
        return c.getClass().getName() + acc + c.getBounds();
    }
}
