import com.sun.source.util.JavacTask;
import java.nio.charset.StandardCharsets;
import java.util.*;
import javax.tools.*;

/** Syntax-only validation; deliberately does not claim Android API type checking. */
public final class ParseSources {
    public static void main(String[] args)throws Exception {
        JavaCompiler compiler=ToolProvider.getSystemJavaCompiler();
        DiagnosticCollector<JavaFileObject> diagnostics=new DiagnosticCollector<>();
        try(StandardJavaFileManager files=compiler.getStandardFileManager(diagnostics,null,StandardCharsets.UTF_8)){
            JavacTask task=(JavacTask)compiler.getTask(null,files,diagnostics,Arrays.asList("--release","8","-proc:none"),null,files.getJavaFileObjects(args));
            task.parse();
            for(Diagnostic<?> d:diagnostics.getDiagnostics())if(d.getKind()==Diagnostic.Kind.ERROR)throw new AssertionError(d);
        }
        System.out.println("PASS: syntax parsed for "+args.length+" Java files (no Android API type check)");
    }
}
