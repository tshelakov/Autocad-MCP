// Optional, in-process COM discovery publisher. No drawing/database access.
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using Autodesk.AutoCAD.Runtime;
using AcApplication = Autodesk.AutoCAD.ApplicationServices.Application;

[assembly: ExtensionApplication(typeof(AutoCadMcp.InstancePublisher))]
[assembly: CommandClass(typeof(AutoCadMcp.InstancePublisher))]

namespace AutoCadMcp
{
    public sealed class InstancePublisher : IExtensionApplication
    {
        private static IRunningObjectTable table;
        private static int? registration;

        [DllImport("ole32.dll", PreserveSig = false)]
        private static extern void GetRunningObjectTable(int reserved, out IRunningObjectTable value);

        [DllImport("ole32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void CreateItemMoniker(string delimiter, string item, out IMoniker value);

        public void Initialize()
        {
            // AcadApplication may not be ready during application startup.
            AcApplication.Idle += PublishWhenIdle;
        }

        private static void PublishWhenIdle(object sender, EventArgs args)
        {
            AcApplication.Idle -= PublishWhenIdle;
            try { Publish(); }
            catch (System.Exception)
            {
                // No modal UI or drawing mutation. MCPREGISTERINSTANCE retries explicitly.
            }
        }

        [CommandMethod("MCPREGISTERINSTANCE", CommandFlags.Session)]
        public static void Publish()
        {
            if (registration.HasValue) return;
            GetRunningObjectTable(0, out table);
            IMoniker moniker;
            CreateItemMoniker("!", "autocad-mcp:pid:" + Process.GetCurrentProcess().Id, out moniker);
            try
            {
                // Register the existing COM object, never a managed wrapper with a new API.
                // RegistrationKeepsAlive=1, no AllowAnyClient: standard user/elevation scope.
                registration = table.Register(1, AcApplication.AcadApplication, moniker);
            }
            finally { Marshal.ReleaseComObject(moniker); }
        }

        [CommandMethod("MCPUNREGISTERINSTANCE", CommandFlags.Session)]
        public static void Unpublish()
        {
            if (registration.HasValue)
            {
                table.Revoke(registration.Value);
                registration = null;
            }
            if (table != null)
            {
                Marshal.ReleaseComObject(table);
                table = null;
            }
        }

        public void Terminate()
        {
            AcApplication.Idle -= PublishWhenIdle;
            Unpublish();
        }
    }
}
