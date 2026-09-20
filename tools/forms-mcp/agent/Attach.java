import com.sun.tools.attach.VirtualMachine;

/** Load an agent into an already-running JVM, by pid. */
public class Attach {
    public static void main(String[] a) throws Exception {
        VirtualMachine vm = VirtualMachine.attach(a[0]);
        try {
            vm.loadAgent(a[1], a[2]);
            System.out.println("agent loaded into pid " + a[0]);
        } finally {
            vm.detach();
        }
    }
}
