import com.chaquo.python.PythonPlugin
import java.security.MessageDigest
import org.gradle.api.artifacts.Configuration

plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

val selectedPython = providers.gradleProperty("pythonVersion").getOrElse("3.14")
require(selectedPython in listOf("3.13", "3.14")) {
    "pythonVersion must be 3.13 or 3.14, not '$selectedPython'"
}
val wheelhouseSetting = providers.gradleProperty("wheelhouse").orNull
    ?: throw GradleException("Pass -Pwheelhouse=/absolute/path/to/verified/wheels")
val wheelhouse = rootProject.file(wheelhouseSetting).canonicalFile
require(wheelhouse.isDirectory && wheelhouse.listFiles().orEmpty().any { it.extension == "whl" }) {
    "wheelhouse must be an existing directory containing verified wheels: $wheelhouse"
}

val generatedRoot = rootProject.file(".generated")
val generatedPython = generatedRoot.resolve("python")
val generatedSmoke = generatedPython.resolve("runtime_smoke.py")
val canonicalSmoke = rootProject.file("../runtime_smoke.py")
val smokeDigest = generatedRoot.resolve("runtime_smoke.sha256")
val generatedVersion = generatedRoot.resolve("python-version.txt")
val generatedWheelhouse = generatedRoot.resolve("wheelhouse.txt")
val bootstrapRoot = providers.gradleProperty("bootstrap").orNull?.let { rootProject.file(it).canonicalFile }
    ?: throw GradleException("Pass -Pbootstrap=/absolute/path/to/verified/fork/bootstrap")
val bootstrapPath = generatedRoot.resolve("bootstrap-path.txt")
val bootstrapVersionFile = generatedRoot.resolve("bootstrap-version.txt")
val bootstrapReceiptHash = generatedRoot.resolve("bootstrap-receipt.sha256")
val bootstrapArchiveHash = generatedRoot.resolve("bootstrap-archive.sha256")
fun digest(file: File): String = MessageDigest.getInstance("SHA-256")
    .digest(file.readBytes()).joinToString("") { "%02x".format(it.toInt() and 0xff) }

require(generatedSmoke.isFile && smokeDigest.isFile && generatedVersion.isFile && generatedWheelhouse.isFile) {
    "Run python compat/hermes/prepare_app.py --python-version $selectedPython " +
        "--wheelhouse \"$wheelhouse\" before building."
}
require(generatedVersion.readText().trim() == selectedPython) {
    "Prepared Python version differs from pythonVersion; rerun prepare_app.py."
}
require(file(generatedWheelhouse.readText().trim()).canonicalFile == wheelhouse) {
    "Prepared wheelhouse differs from -Pwheelhouse; rerun prepare_app.py."
}
require(digest(generatedSmoke) == smokeDigest.readText().trim() && digest(canonicalSmoke) == digest(generatedSmoke)) {
    "Generated runtime_smoke.py is stale or modified; rerun prepare_app.py."
}
require(listOf(bootstrapPath, bootstrapVersionFile, bootstrapReceiptHash, bootstrapArchiveHash).all { it.isFile }) {
    "Run prepare_app.py with --bootstrap before building the fork runtime."
}
val bootstrapVersion = bootstrapVersionFile.readText().trim()
require(rootProject.file(bootstrapPath.readText().trim()).canonicalFile == bootstrapRoot &&
    bootstrapVersion == rootProject.file("../../../VERSION.txt").readText().trim() &&
    digest(bootstrapRoot.resolve("receipt.json")) == bootstrapReceiptHash.readText().trim()) {
    "The prepared fork bootstrap path, version, or receipt changed; rerun prepare_app.py."
}
val expectedBootstrapHash = bootstrapArchiveHash.readText().trim()
require(expectedBootstrapHash.matches(Regex("[0-9a-f]{64}"))) { "Invalid bootstrap archive digest" }

android {
    namespace = "com.mobilefork.chaquopycompat"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.mobilefork.chaquopycompat"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1-python$selectedPython"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

chaquopy {
    defaultConfig {
        version = selectedPython
        providers.gradleProperty("buildPython").orNull?.let { buildPython(it) }
        pip {
            // Never build an unplanned native sdist or fall back to a Hermes
            // stub. jiter/core and pure msgpack come from our verified artifacts;
            // aiohttp and the remaining genuine PyPI distributions are already
            // hash-checked in the complete offline wheelhouse.
            options("--no-index", "--find-links", wheelhouse.absolutePath,
                "--only-binary", ":all:")
            install("-r", rootProject.file("requirements.txt").absolutePath)
        }
    }
    sourceSets.getByName("main") {
        srcDir(generatedPython)
    }
}

dependencies {
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
}

// Chaquopy 17 keeps runtime dependencies in its own detached configurations.
// Only substitute the source-built Python bootstrap: Java/JNI stay on stable 17.2.0.
afterEvaluate {
    val pythonPlugin = plugins.getPlugin(PythonPlugin::class.java)
    val bootstrapConfigs = pythonPlugin.configs.filterKeys { it.endsWith("RuntimeBootstrap") }
    check(bootstrapConfigs.keys == setOf("pythonDebugRuntimeBootstrap", "pythonReleaseRuntimeBootstrap")) {
        "Chaquopy's pinned bootstrap configuration contract changed: ${bootstrapConfigs.keys}"
    }
    bootstrapConfigs.values.forEach { config ->
        check(config.state == Configuration.State.UNRESOLVED) { "Bootstrap resolved before fork selection" }
        config.resolutionStrategy.eachDependency {
            if (requested.group == "com.chaquo.python.runtime" && requested.name == "bootstrap") {
                useVersion(bootstrapVersion)
                because("Qualify the hash-verified fork importer with stable Chaquopy Java/JNI")
            }
        }
    }
    val verifyForkBootstrap = tasks.register("verifyForkBootstrap") {
        doLast {
            bootstrapConfigs.values.forEach { config ->
                val artifact = config.resolvedConfiguration.resolvedArtifacts.single()
                val identity = artifact.moduleVersion.id
                check(identity.group == "com.chaquo.python.runtime" && identity.name == "bootstrap" &&
                    identity.version == bootstrapVersion && artifact.classifier == selectedPython &&
                    artifact.extension == "imy" && digest(artifact.file) == expectedBootstrapHash) {
                    "Resolved bootstrap differs from the verified fork artifact: $identity"
                }
            }
        }
    }
    tasks.named("preBuild") { dependsOn(verifyForkBootstrap) }
}
