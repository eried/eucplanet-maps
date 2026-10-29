import btools.mapaccess.PhysicalFile;
import java.io.File;

/** Runs BRouter's own integrity check (every index and data checksum) on
 *  each rd5 named on the command line. Prints OK or BAD per file and
 *  exits 1 when any is bad. */
public class CheckTiles {
  public static void main(String[] args) {
    int bad = 0;
    for (String path : args) {
      File f = new File(path);
      try {
        PhysicalFile.checkFileIntegrity(f);
        System.out.println("OK " + f.getName());
      } catch (Exception e) {
        bad++;
        System.out.println("BAD " + f.getName() + " " + e);
      }
    }
    System.exit(bad == 0 ? 0 : 1);
  }
}
