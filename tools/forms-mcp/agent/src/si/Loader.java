package com.aitest.si;

import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.util.jar.JarFile;

/**
 * Stage-by-stage loader, to locate a failure that cannot be printed.
 *
 * The bootstrap-loading approach works under a mimic of the Web Start sandbox
 * but fails inside the real Forms client, whose stderr goes nowhere visible and
 * whose Attach API reports only "failed to initialize". Returning normally is
 * the one signal that does escape, so each stage stops just after one step: the
 * last stage that reports "agent loaded" is the last step that worked.
 *
 * args: "<jarPath>|<port>|<secret>|<stage>"
 */
public class Loader {

    public static void agentmain(String args, Instrumentation inst)
            throws Exception {
        String[] a = args.trim().split("[|]");
        String jarPath = a[0];
        String port = a.length > 1 ? a[1] : "8137";
        String secret = a.length > 2 ? a[2] : "";
        int stage = a.length > 3 ? Integer.parseInt(a[3]) : 9;

        if (stage <= 0) {
            return;
        }
        JarFile jf = new JarFile(jarPath);
        if (stage <= 1) {
            return;
        }
        inst.appendToBootstrapClassLoaderSearch(jf);
        if (stage <= 2) {
            return;
        }
        Class<?> driver = Class.forName("com.aitest.si.Driver");
        if (stage <= 3) {
            return;
        }
        if (driver.getClassLoader() != null) {
            throw new IllegalStateException("not bootstrap-loaded");
        }
        if (stage <= 4) {
            return;
        }
        Method start = driver.getMethod("start", String.class, String.class);
        if (stage <= 5) {
            return;
        }
        start.invoke(null, port, secret);
    }
}
