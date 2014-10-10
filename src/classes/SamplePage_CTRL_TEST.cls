@isTest
public with sharing class SamplePage_CTRL_TEST {

    public static testmethod void getSamplesTest() {
    	SamplePage_CTRL controller = new SamplePage_CTRL();
    	
    	SampleObject__c sample = new SampleObject__c(Sample_Field__c = 'MyData');
    	insert  sample;
    	
    	List<SampleObject__c> samples = controller.getSamples();
    	System.assertEquals(1, samples.size());
    	System.assertEquals('MyData', samples[0].Sample_Field__c);
    }
}