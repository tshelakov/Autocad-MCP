// In-process COM discovery publisher. No drawing/database access.
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
        private static int attempts;

        [DllImport("ole32.dll", PreserveSig = false)]
        private static extern void GetRunningObjectTable(int reserved, out IRunningObjectTable value);

        [DllImport("ole32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void CreateItemMoniker(string delimiter, string item, out IMoniker value);

        public void Initialize()
        {
            attempts = 0;
            AcApplication.Idle += PublishWhenIdle;
        }

        private static void PublishWhenIdle(object sender, EventArgs args)
        {
            try
            {
                Publish();
                AcApplication.Idle -= PublishWhenIdle;
            }
            catch (System.Exception error)
            {
                // Startup may not expose AcadApplication yet. Bounded, on the main thread.
                if (++attempts >= 10)
                {
                    AcApplication.Idle -= PublishWhenIdle;
                    Trace.WriteLine("AutoCAD MCP instance publisher: " + error.Message);
                }
            }
        }

        [CommandMethod("MCPREGISTERINSTANCE", CommandFlags.Session)]
        public static void Publish()
        {
            if (registration.HasValue) return;
            IRunningObjectTable nextTable;
            GetRunningObjectTable(0, out nextTable);
            IMoniker moniker = null;
            try
            {
                CreateItemMoniker("!", "autocad-mcp:pid:" + Process.GetCurrentProcess().Id, out moniker);
                // Existing COM object, not a managed wrapper. No AllowAnyClient flag.
                registration = nextTable.Register(1, AcApplication.AcadApplication, moniker);
                table = nextTable;
                nextTable = null;
            }
            finally
            {
                if (moniker != null) Marshal.ReleaseComObject(moniker);
                if (nextTable != null) Marshal.ReleaseComObject(nextTable);
            }
        }

        [CommandMethod("MCPUNREGISTERINSTANCE", CommandFlags.Session)]
        public static void Unpublish()
        {
            AcApplication.Idle -= PublishWhenIdle;
            try
            {
                if (registration.HasValue) table.Revoke(registration.Value);
            }
            finally
            {
                registration = null;
                if (table != null) Marshal.ReleaseComObject(table);
                table = null;
            }
        }

        public void Terminate()
        {
            Unpublish();
        }
    }
}
