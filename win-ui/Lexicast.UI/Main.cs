using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.Windows.ApplicationModel.DynamicDependency;

namespace Lexicast.UI
{
    /// <summary>
    /// Replaces the XAML-generated Main (disabled via DISABLE_XAML_GENERATED_MAIN) so the app
    /// can run both packaged (MSIX) and unpackaged. Unpackaged launches have no package identity,
    /// so the Windows App SDK runtime must be located explicitly via the Bootstrap API before
    /// Microsoft.UI.Xaml.Application.Start is called, otherwise activation fails with a COMException.
    /// </summary>
    public static class Program
    {
        private const int APPMODEL_ERROR_NO_PACKAGE = 15700;
        private const uint WindowsAppSdkMajorMinorVersion = 0x00020002; // 2.2

        [DllImport("kernel32.dll")]
        private static extern int GetCurrentPackageFullName(ref int packageFullNameLength, StringBuilder? packageFullName);

        private static bool HasPackageIdentity()
        {
            int length = 0;
            int result = GetCurrentPackageFullName(ref length, null);
            return result != APPMODEL_ERROR_NO_PACKAGE;
        }

        [STAThread]
        private static void Main(string[] args)
        {
            WinRT.ComWrappersSupport.InitializeComWrappers();

            bool bootstrapped = false;
            if (!HasPackageIdentity())
            {
                bootstrapped = Bootstrap.TryInitialize(WindowsAppSdkMajorMinorVersion, out int hresult);
                if (!bootstrapped)
                {
                    throw new InvalidOperationException(
                        $"Failed to initialize the Windows App SDK runtime (HRESULT 0x{hresult:X8}). " +
                        "Make sure the Windows App Runtime matching this app's Microsoft.WindowsAppSDK package version is installed.");
                }
            }

            try
            {
                Application.Start(p =>
                {
                    var context = new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread());
                    SynchronizationContext.SetSynchronizationContext(context);
                    new App();
                });
            }
            finally
            {
                if (bootstrapped)
                {
                    Bootstrap.Shutdown();
                }
            }
        }
    }
}
