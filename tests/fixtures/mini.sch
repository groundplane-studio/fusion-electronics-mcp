<?xml version="1.0" encoding="utf-8"?>
<eagle version="9.7.0">
<drawing>
<layers><layer number="91" name="Nets" color="2" fill="1" visible="yes" active="yes"/></layers>
<schematic>
<libraries>
<library name="TESTLIB" urn="urn:test:lib">
<symbols>
<symbol name="RES">
<pin name="P$1" x="0" y="0" length="short" direction="pas"/>
<pin name="P$2" x="10.16" y="0" length="short" direction="pas" rot="R180"/>
</symbol>
<symbol name="REG">
<pin name="VIN" x="-7.62" y="0" length="short" direction="pwr"/>
<pin name="VOUT" x="7.62" y="0" length="short" direction="out" rot="R180"/>
<pin name="EN" x="-7.62" y="-2.54" length="short" direction="in"/>
<pin name="NC" x="7.62" y="-2.54" length="short" direction="pas" rot="R180"/>
</symbol>
</symbols>
<devicesets>
<deviceset name="RES_0402" prefix="R">
<gates><gate name="G$1" symbol="RES" x="0" y="0"/></gates>
<devices>
<device name="_10K" package="R0402">
<connects><connect gate="G$1" pin="P$1" pad="1"/><connect gate="G$1" pin="P$2" pad="2"/></connects>
<technologies><technology name=""><attribute name="JLCPCB" value="C25744"/><attribute name="VALUE" value="10k"/></technology></technologies>
</device>
<device name="_1K" package="R0402">
<connects><connect gate="G$1" pin="P$1" pad="1"/><connect gate="G$1" pin="P$2" pad="2"/></connects>
<technologies><technology name=""><attribute name="JLCPCB" value="C11702"/></technology></technologies>
</device>
</devices>
</deviceset>
<deviceset name="LDO" prefix="U" uservalue="yes">
<gates><gate name="G$1" symbol="REG" x="0" y="0"/></gates>
<devices>
<device name="" package="SOT23">
<connects><connect gate="G$1" pin="VIN" pad="1"/><connect gate="G$1" pin="VOUT" pad="2"/><connect gate="G$1" pin="EN" pad="3"/><connect gate="G$1" pin="NC" pad="4"/></connects>
<technologies><technology name=""/></technologies>
</device>
</devices>
</deviceset>
</devicesets>
</library>
</libraries>
<classes><class number="0" name="default" width="0" drill="0"/></classes>
<parts>
<part name="R1" library="TESTLIB" library_urn="urn:test:lib" deviceset="RES_0402" device="_10K" value="10k"/>
<part name="R2" library="TESTLIB" library_urn="urn:test:lib" deviceset="RES_0402" device="_1K" value="1k"/>
<part name="U1" library="TESTLIB" library_urn="urn:test:lib" deviceset="LDO" device="" value=""/>
</parts>
<sheets>
<sheet>
<instances>
<instance part="R1" gate="G$1" x="0" y="0" rot="R90"/>
<instance part="R2" gate="G$1" x="20" y="0"/>
<instance part="U1" gate="G$1" x="40" y="0" rot="MR0"/>
</instances>
<nets>
<net name="VOUT" class="0">
<segment>
<pinref part="R1" gate="G$1" pin="P$2"/>
<wire x1="0" y1="10.16" x2="20" y2="0" width="0.1524" layer="91"/>
<pinref part="R2" gate="G$1" pin="P$1"/>
</segment>
</net>
</nets>
</sheet>
<sheet>
<instances/>
<nets>
<net name="VOUT" class="0">
<segment>
<pinref part="U1" gate="G$1" pin="VOUT"/>
<wire x1="32.38" y1="0" x2="30" y2="0" width="0.1524" layer="91"/>
</segment>
</net>
</nets>
</sheet>
</sheets>
</schematic>
</drawing>
</eagle>
