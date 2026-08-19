package com.aptitudelab.mobile

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.edit
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import com.google.android.gms.auth.api.identity.AuthorizationRequest
import com.google.android.gms.auth.api.identity.AuthorizationResult
import com.google.android.gms.auth.api.identity.Identity
import com.google.android.gms.auth.api.identity.RevokeAccessRequest
import com.google.android.gms.auth.api.signin.GoogleSignInAccount
import com.google.android.gms.common.api.ApiException
import com.google.android.gms.common.api.Scope
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.UUID

class MainActivity : ComponentActivity() {
    companion object {
        private val serverLock = Any()
        @Volatile private var serverThreadStarted = false
        private var processAppSecret: String? = null
    }

    private lateinit var webView: WebView
    private lateinit var statusView: TextView
    private lateinit var appSecret: String
    private var authorizedAccount: GoogleSignInAccount? = null

    private val driveScopes = listOf(
        Scope("https://www.googleapis.com/auth/drive.readonly"),
        Scope("openid"),
        Scope("email"),
        Scope("profile"),
    )

    private val authorizationLauncher = registerForActivityResult(
        ActivityResultContracts.StartIntentSenderForResult()
    ) { activityResult ->
        if (activityResult.resultCode != Activity.RESULT_OK || activityResult.data == null) {
            notifyAuthorizationFailure("Google authorization was cancelled.")
            return@registerForActivityResult
        }
        try {
            val result = Identity.getAuthorizationClient(this)
                .getAuthorizationResultFromIntent(activityResult.data!!)
            handleAuthorizationResult(result)
        } catch (error: ApiException) {
            notifyAuthorizationFailure("Google authorization failed (${error.statusCode}).")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        webView = findViewById(R.id.web_view)
        statusView = findViewById(R.id.startup_status)
        synchronized(serverLock) {
            if (processAppSecret == null) {
                processAppSecret = UUID.randomUUID().toString() + UUID.randomUUID().toString()
            }
            appSecret = processAppSecret!!
        }
        configureWebView()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        Thread {
            try {
                val staticDirectory = File(filesDir, "web")
                prepareWebAssets(staticDirectory)
                val dataDirectory = File(filesDir, "aptitude-data").apply { mkdirs() }
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(this))
                }
                synchronized(serverLock) {
                    if (!serverThreadStarted) {
                        serverThreadStarted = true
                        Thread {
                            try {
                                Python.getInstance().getModule("mobile_runtime").callAttr(
                                    "start_server",
                                    dataDirectory.absolutePath,
                                    staticDirectory.absolutePath,
                                    appSecret,
                                    BuildConfig.DRIVE_FOLDER_URL,
                                )
                            } catch (error: Throwable) {
                                showStartupFailure("The embedded aptitude engine stopped: ${error.message ?: error.javaClass.simpleName}")
                            } finally {
                                serverThreadStarted = false
                            }
                        }.start()
                    }
                }
                waitForServer()
            } catch (error: Throwable) {
                showStartupFailure("Unable to start Aptitude Mobile: ${error.message ?: error.javaClass.simpleName}")
            }
        }.start()
    }

    @SuppressLint("SetJavaScriptEnabled", "AddJavascriptInterface")
    private fun configureWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            cacheMode = WebSettings.LOAD_DEFAULT
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            setSupportZoom(false)
        }
        webView.webChromeClient = WebChromeClient()
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val uri = request?.url ?: return true
                return uri.scheme != "http" || uri.host != "127.0.0.1"
            }
        }
        webView.addJavascriptInterface(AndroidBridge(), "AndroidBridge")
    }

    private fun prepareWebAssets(destination: File) {
        val preferences = getSharedPreferences("aptitude-mobile", MODE_PRIVATE)
        if (preferences.getInt("web-version", -1) == BuildConfig.VERSION_CODE && destination.isDirectory) {
            return
        }
        if (destination.exists()) destination.deleteRecursively()
        destination.mkdirs()
        copyAssetDirectory("web", destination)
        preferences.edit { putInt("web-version", BuildConfig.VERSION_CODE) }
    }

    private fun copyAssetDirectory(assetPath: String, destination: File) {
        val children = assets.list(assetPath).orEmpty()
        if (children.isEmpty()) {
            destination.parentFile?.mkdirs()
            assets.open(assetPath).use { input -> destination.outputStream().use(input::copyTo) }
            return
        }
        destination.mkdirs()
        children.forEach { child ->
            copyAssetDirectory("$assetPath/$child", File(destination, child))
        }
    }

    private fun waitForServer() {
        repeat(150) {
            try {
                val connection = URI("http://127.0.0.1:8000/api/mobile/config").toURL()
                    .openConnection() as HttpURLConnection
                connection.connectTimeout = 300
                connection.readTimeout = 300
                connection.useCaches = false
                if (connection.responseCode == 200) {
                    connection.disconnect()
                    runOnUiThread {
                        statusView.visibility = View.GONE
                        webView.visibility = View.VISIBLE
                        val encoded = URLEncoder.encode(appSecret, StandardCharsets.UTF_8.name())
                        webView.loadUrl("http://127.0.0.1:8000/?mobileToken=$encoded")
                    }
                    return
                }
                connection.disconnect()
            } catch (_: Exception) {
                // The embedded server is still starting.
            }
            Thread.sleep(200)
        }
        showStartupFailure("The embedded aptitude engine did not start within 30 seconds.")
    }

    private fun requestAuthorization(forceAccountPicker: Boolean) {
        val builder = AuthorizationRequest.builder().setRequestedScopes(driveScopes)
        if (forceAccountPicker) builder.setPrompt(AuthorizationRequest.Prompt.SELECT_ACCOUNT)
        Identity.getAuthorizationClient(this)
            .authorize(builder.build())
            .addOnSuccessListener(::handleAuthorizationResult)
            .addOnFailureListener { error ->
                notifyAuthorizationFailure(error.message ?: "Google authorization is unavailable on this device.")
            }
    }

    private fun handleAuthorizationResult(result: AuthorizationResult) {
        if (result.hasResolution()) {
            val pendingIntent = result.pendingIntent
            if (pendingIntent == null) {
                notifyAuthorizationFailure("Google authorization could not open the account chooser.")
                return
            }
            authorizationLauncher.launch(IntentSenderRequest.Builder(pendingIntent.intentSender).build())
            return
        }
        val accessToken = result.accessToken
        if (accessToken.isNullOrBlank()) {
            notifyAuthorizationFailure("Google did not return an access token.")
            return
        }
        authorizedAccount = result.toGoogleSignInAccount()
        val payload = JSONObject()
            .put("accessToken", accessToken)
            .put("email", authorizedAccount?.email ?: "")
            .put("name", authorizedAccount?.displayName ?: "")
        runOnUiThread {
            webView.evaluateJavascript("window.aptitudeMobileDriveAuthorized($payload);", null)
        }
    }

    private fun revokeAuthorization() {
        val account = authorizedAccount?.account ?: return
        val request = RevokeAccessRequest.builder()
            .setAccount(account)
            .setScopes(driveScopes)
            .build()
        Identity.getAuthorizationClient(this)
            .revokeAccess(request)
            .addOnCompleteListener { authorizedAccount = null }
    }

    private fun notifyAuthorizationFailure(message: String) {
        val encoded = JSONObject.quote(message)
        runOnUiThread {
            webView.evaluateJavascript("window.aptitudeMobileDriveAuthorizationFailed($encoded);", null)
        }
    }

    private fun showStartupFailure(message: String) {
        runOnUiThread {
            statusView.text = message
            statusView.visibility = View.VISIBLE
            webView.visibility = View.GONE
        }
    }

    inner class AndroidBridge {
        @JavascriptInterface
        fun authorizeDrive(forceAccountPicker: Boolean) {
            runOnUiThread { requestAuthorization(forceAccountPicker) }
        }

        @JavascriptInterface
        fun disconnectDrive() {
            runOnUiThread { revokeAuthorization() }
        }
    }
}
